// agentkvmd - a small HID daemon for IP-KVM boards, for agent control.
//
// It writes USB HID reports straight to the gadget character devices the
// firmware exposes (/dev/hidg0 keyboard, and the absolute pointer on the node
// the board's profile names, /dev/hidg2 on the NanoKVM) and serves
// one authenticated JSON API over the LAN. It does NOT talk to the stock
// firmware's control APIs: those cannot express a modified keypress or a held
// key at all (their /keyboard/key hard-codes modifier 0, and their combo verb
// always taps), so anything above them loses the ability to hold ctrl, or to
// press ctrl+shift+esc.
//
// Two deliberate properties:
//
//   - Auth is MANDATORY and has no localhost exemption. This daemon types on a
//     real machine; it refuses to start without a key and checks every request.
//   - A held key is released by a watchdog if the caller goes away. A stuck
//     key auto-repeats on the target forever and nothing on screen says why.
package main

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"
)

// A gadget profile is the whole of what differs between boards. Both expose a
// standard 8-byte boot keyboard; they disagree about which node the absolute
// pointer lives on and what its report looks like.
type profile struct {
	name      string
	kbdPath   string
	mousePath string
	// absReport builds one absolute-pointer report for this board's layout.
	absReport func(buttons byte, x, y int) []byte
	// wheelReport builds a separate wheel report, or is nil when the wheel is
	// a field of the pointer report (as on the NanoKVM).
	wheelReport func(dy int8) []byte
}

var profiles = map[string]profile{
	// Sipeed NanoKVM. The absolute pointer is the TOUCHPAD function on hidg2 -
	// hidg1 there is a RELATIVE mouse, and writing an absolute report to it
	// produces plausible-looking nonsense rather than an error.
	"nanokvm": {
		name: "nanokvm", kbdPath: "/dev/hidg0", mousePath: "/dev/hidg2",
		absReport: func(b byte, x, y int) []byte {
			return []byte{b, byte(x), byte(x >> 8), byte(y), byte(y >> 8), 0}
		},
		wheelReport: nil, // carried in the pointer report's last byte
	},
}

const (
	absMax = 32767 // both axes, matching the gadget's Logical Maximum

	// A key held past the target's auto-repeat delay (~250 ms on Windows)
	// duplicates the character: `notepad` arrives as `noteeeeeeeeeeeepad`.
	// The whole point of doing the tap on this box rather than on the calling
	// host is that the hold cannot stretch under the caller's load.
	defaultHoldMs = 40
	maxHoldMs     = 200

	// If a report leaves keys or buttons down and nothing follows, release
	// them. Covers a caller that crashed, or a network that went away, mid-hold.
	stuckTimeout = 5 * time.Second
)

// hid owns the two gadget files. Writes are serialised: the reports are
// stateful (a keyboard report REPLACES what is held), so an interleave would
// silently lose a modifier.
type hid struct {
	p     profile
	mu    sync.Mutex
	kbd   *os.File
	mouse *os.File

	held    bool      // something is down right now
	lastAct time.Time // when the last report went out
	lastX   int       // last commanded pointer position: an absolute report
	lastY   int       // always carries one, so releasing a button re-sends it

	// stuck holds a gadget file whose write the kernel has kept past the
	// deadline. It is not written again until that write returns.
	stuck map[*os.File]bool
}

func (h *hid) open() error {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.openLocked()
}

func (h *hid) openLocked() error {
	var err error
	if h.kbd == nil {
		if h.kbd, err = os.OpenFile(h.p.kbdPath, os.O_RDWR|syscall.O_NONBLOCK, 0); err != nil {
			return fmt.Errorf("open %s: %w", h.p.kbdPath, err)
		}
	}
	if h.mouse == nil {
		if h.mouse, err = os.OpenFile(h.p.mousePath, os.O_RDWR|syscall.O_NONBLOCK, 0); err != nil {
			return fmt.Errorf("open %s: %w", h.p.mousePath, err)
		}
	}
	return nil
}

// ErrNotPolled: the host has enumerated the gadget but is not reading this
// interface, so a report has nowhere to go. Seen on the first Windows target:
// it polls the keyboard and the relative mouse and never the absolute
// pointer, and a blocking write there hung the daemon before it listened.
var ErrNotPolled = errors.New("host is not polling this HID interface (target asleep or USB suspended, or no reader on the other end of the cable)")

// reportTimeout is how long a report may wait for the host to poll the
// endpoint before we call it unread. A host that is reading polls every
// 1-8 ms; 300 ms is a dead interface, not a slow one.
const reportTimeout = 300 * time.Millisecond

// stuckGrace is how far past reportTimeout a write may run before it is left
// behind. The deadline only covers waiting in Go's poller: when the target
// sleeps with remote wakeup refused, the NanoKVM's 5.10 hidg driver stays
// inside write() retrying the wakeup, non-blocking or not, and held hid.mu
// with it. Every later key then waited behind one mouse report for good.
const stuckGrace = 200 * time.Millisecond

// write sends one report, reopening the gadget once if the target
// re-enumerated the USB device underneath us (a reboot, or a UAC prompt
// switching desktops, both do this). The gadget nodes are opened
// non-blocking so the write goes through Go's poller and honours a deadline:
// a gadget write otherwise blocks for as long as the host declines to poll,
// which can be forever.
func (h *hid) write(f **os.File, report []byte) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	for attempt := 0; attempt < 2; attempt++ {
		if err := h.openLocked(); err != nil {
			return err
		}
		file := *f
		if h.stuck[file] {
			return ErrNotPolled
		}
		file.SetWriteDeadline(time.Now().Add(reportTimeout))
		done := make(chan error, 1)
		go func() { _, err := file.Write(report); done <- err }()
		var err error
		select {
		case err = <-done:
		case <-time.After(reportTimeout + stuckGrace):
			h.leaveBehind(file, done)
			return ErrNotPolled
		}
		if err == nil {
			h.lastAct = time.Now()
			return nil
		}
		if errors.Is(err, os.ErrDeadlineExceeded) || errors.Is(err, syscall.EAGAIN) {
			return ErrNotPolled // reopening would not make the host read it
		}
		(*f).Close()
		*f = nil
		if attempt == 1 {
			return err
		}
	}
	return errors.New("unreachable")
}

// leaveBehind sets aside a file whose write the kernel is holding, with
// hid.mu held. When the write finally returns (the target woke) its report
// lands late, so whatever it pressed is marked held and the watchdog
// releases it on its next tick.
func (h *hid) leaveBehind(file *os.File, done <-chan error) {
	if h.stuck == nil {
		h.stuck = map[*os.File]bool{}
	}
	h.stuck[file] = true
	log.Printf("hid: a write to %s is stuck in the kernel (target asleep?); failing reports to it until it returns", file.Name())
	go func() {
		<-done
		h.mu.Lock()
		delete(h.stuck, file)
		h.held, h.lastAct = true, time.Time{}
		h.mu.Unlock()
		log.Printf("hid: the stuck write to %s returned", file.Name())
	}()
}

// Keyboard sends a boot-protocol report: a modifier bitmap and up to six
// usages. Zero keys with zero modifier is the release.
func (h *hid) Keyboard(mod byte, keys []byte) error {
	report := make([]byte, 8)
	report[0] = mod
	for i := 0; i < len(keys) && i < 6; i++ {
		report[2+i] = keys[i]
	}
	err := h.write(&h.kbd, report)
	h.setHeld(mod != 0 || len(keys) > 0)
	return err
}

// Mouse is absolute: x and y are 0..absMax on each axis, origin top-left.
func (h *hid) Mouse(buttons byte, x, y int) error {
	x, y = clamp(x), clamp(y)
	h.mu.Lock()
	h.lastX, h.lastY = x, y
	h.mu.Unlock()
	err := h.write(&h.mouse, h.p.absReport(buttons, x, y))
	h.setHeld(buttons != 0)
	return err
}

func (h *hid) Wheel(dy int8) error {
	if dy == 0 {
		return nil
	}
	if h.p.wheelReport == nil {
		// The wheel rides in this board's pointer report, so scrolling means
		// re-sending the current position with a wheel delta on the end.
		h.mu.Lock()
		x, y := h.lastX, h.lastY
		h.mu.Unlock()
		r := h.p.absReport(0, x, y)
		r[len(r)-1] = byte(dy)
		return h.write(&h.mouse, r)
	}
	return h.write(&h.mouse, h.p.wheelReport(dy))
}

func (h *hid) ReleaseAll() error {
	e1 := h.Keyboard(0, nil)
	h.mu.Lock()
	x, y := h.lastX, h.lastY
	h.mu.Unlock()
	e2 := h.Mouse(0, x, y) // buttons up, pointer left where it was
	h.setHeld(false)
	if e1 != nil {
		return e1
	}
	return e2
}

func (h *hid) setHeld(v bool) {
	h.mu.Lock()
	h.held = v
	h.mu.Unlock()
}

// watchdog releases anything left down by a caller that stopped talking.
func (h *hid) watchdog() {
	for range time.Tick(time.Second) {
		h.mu.Lock()
		stuck := h.held && time.Since(h.lastAct) > stuckTimeout
		h.mu.Unlock()
		if stuck {
			log.Printf("watchdog: releasing keys held for >%s", stuckTimeout)
			h.ReleaseAll()
		}
	}
}

func clamp(v int) int {
	if v < 0 {
		return 0
	}
	if v > absMax {
		return absMax
	}
	return v
}

// --- HTTP ---

type server struct {
	hid *hid
	key string
	// The stock firmware serves JPEG captures on loopback. Proxying it here
	// means a caller needs one address and one credential for eyes and hands.
	captureURL string
	// Where /v1/pair checks that a caller's web-portal token is valid: the
	// stock firmware's own API, on loopback. A 200 there means the caller is
	// signed in to the board as an admin, which is the authority /v1/pair
	// trades for the API key.
	verifyURL string

	// /v1/pair hands out the key, so a flood of guesses is throttled. Tokens
	// are JWTs and not guessable, but each attempt also hits the stock API, so
	// this is as much to spare that as to resist anything.
	pairMu   sync.Mutex
	pairNext time.Time

	// kbdSeq orders whole keyboard operations. hid.mu serialises single
	// reports, but a tap is press, hold, release, and each report replaces
	// what is held, so a report from another caller during the hold would be
	// lost or released early. Tap holds this for the whole sequence; keys and
	// release take it for theirs.
	kbdSeq sync.Mutex

	// captureTimeout bounds a /v1/screenshot fetch from the stock capture.
	captureTimeout time.Duration
}

func (s *server) auth(r *http.Request) bool {
	got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	// Constant time, and case-sensitive: the stock servers use EqualFold,
	// which throws away a bit per alphabetic character of an operator-set key.
	return subtle.ConstantTimeCompare([]byte(strings.TrimSpace(got)), []byte(s.key)) == 1
}

// portalAccepts reports whether the stock web portal considers this token a
// signed-in session, by making an authenticated call to it on loopback. The
// daemon does not know the portal's JWT signing key and does not try to: it
// asks the thing that issued the token whether the token is still good.
func (s *server) portalAccepts(token string) bool {
	req, err := http.NewRequest(http.MethodGet, s.verifyURL, nil)
	if err != nil {
		return false
	}
	// The stock portal reads the token from this cookie; some builds also read
	// the Authorization header, so send both.
	req.AddCookie(&http.Cookie{Name: "nano-kvm-token", Value: token})
	req.Header.Set("Authorization", token)

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("pair: could not reach the portal at %s to verify: %v", s.verifyURL, err)
		return false
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
	return resp.StatusCode == http.StatusOK
}

func (s *server) handle(fn func(*http.Request) (any, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !s.auth(r) {
			http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		if r.Method != http.MethodPost {
			http.Error(w, `{"error":"POST only"}`, http.StatusMethodNotAllowed)
			return
		}
		out, err := fn(r)
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			code := http.StatusBadRequest
			if errors.Is(err, ErrNotPolled) {
				code = http.StatusServiceUnavailable // the cable, not the request
			}
			w.WriteHeader(code)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		if out == nil {
			out = map[string]bool{"ok": true}
		}
		json.NewEncoder(w).Encode(out)
	}
}

func decode(r *http.Request, v any) error {
	defer r.Body.Close()
	return json.NewDecoder(io.LimitReader(r.Body, 1<<16)).Decode(v)
}

func (s *server) routes() *http.ServeMux {
	mux := http.NewServeMux()

	// Unauthenticated, and deliberately says nothing about the device.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"ok":true}`)
	})

	mux.HandleFunc("/v1/mouse", s.handle(func(r *http.Request) (any, error) {
		var q struct {
			Buttons byte `json:"buttons"`
			X       *int `json:"x"`
			Y       *int `json:"y"`
		}
		if err := decode(r, &q); err != nil {
			return nil, err
		}
		if q.X == nil || q.Y == nil {
			return nil, errors.New("x and y are required")
		}
		// Note: 0 is a legal coordinate here. The stock LAN API tags its x/y
		// `binding:"required"`, which makes Go's binder reject zero as absent,
		// so the top row and left column are unaddressable through it.
		return nil, s.hid.Mouse(q.Buttons, *q.X, *q.Y)
	}))

	mux.HandleFunc("/v1/wheel", s.handle(func(r *http.Request) (any, error) {
		var q struct {
			Dy int8 `json:"dy"`
		}
		if err := decode(r, &q); err != nil {
			return nil, err
		}
		return nil, s.hid.Wheel(q.Dy)
	}))

	// Raw report: the full contract. Hold ctrl, press six keys, whatever the
	// caller's model of the keyboard says.
	mux.HandleFunc("/v1/keys", s.handle(func(r *http.Request) (any, error) {
		var q struct {
			Modifier byte   `json:"modifier"`
			Keys     []byte `json:"keys"`
		}
		if err := decode(r, &q); err != nil {
			return nil, err
		}
		if len(q.Keys) > 6 {
			return nil, errors.New("a boot keyboard report carries at most six keys")
		}
		s.kbdSeq.Lock()
		defer s.kbdSeq.Unlock()
		return nil, s.hid.Keyboard(q.Modifier, q.Keys)
	}))

	// One atomic tap. The press and the release happen here, microseconds
	// apart, rather than as two round trips across a network from a host that
	// might be under load.
	mux.HandleFunc("/v1/tap", s.handle(func(r *http.Request) (any, error) {
		var q struct {
			Modifier byte   `json:"modifier"`
			Keys     []byte `json:"keys"`
			HoldMs   int    `json:"hold_ms"`
		}
		if err := decode(r, &q); err != nil {
			return nil, err
		}
		if len(q.Keys) > 6 {
			return nil, errors.New("a boot keyboard report carries at most six keys")
		}
		hold := q.HoldMs
		if hold <= 0 {
			hold = defaultHoldMs
		}
		if hold > maxHoldMs {
			hold = maxHoldMs // never long enough to trip the target's auto-repeat
		}
		s.kbdSeq.Lock()
		defer s.kbdSeq.Unlock()
		if err := s.hid.Keyboard(q.Modifier, q.Keys); err != nil {
			return nil, err
		}
		time.Sleep(time.Duration(hold) * time.Millisecond)
		return map[string]any{"ok": true, "held_ms": hold}, s.hid.Keyboard(0, nil)
	}))

	mux.HandleFunc("/v1/release", s.handle(func(r *http.Request) (any, error) {
		s.kbdSeq.Lock()
		defer s.kbdSeq.Unlock()
		return nil, s.hid.ReleaseAll()
	}))

	// Eyes, proxied from the stock firmware's loopback capture.
	// Pairing. Not behind the API key, by design: it is how a client that does
	// not yet have the key obtains it. The proof of authority is a valid
	// web-portal session, which the daemon confirms by calling the stock API on
	// loopback with the caller's token. Someone who can sign in to the board's
	// portal can already read the key file over SSH, so handing it to them here
	// grants nothing they lacked; it just spares a human copying 64 hex chars.
	mux.HandleFunc("/v1/pair", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodPost {
			http.Error(w, `{"error":"POST only"}`, http.StatusMethodNotAllowed)
			return
		}

		// Throttle: one attempt per 500 ms, so a token-guessing loop cannot run
		// flat out and cannot hammer the stock API either.
		// Slots are cumulative: the Nth concurrent attempt waits N*500 ms,
		// rather than every one of them waiting at most 500 ms and then
		// arriving together.
		s.pairMu.Lock()
		now := time.Now()
		if s.pairNext.Before(now) {
			s.pairNext = now
		}
		wait := s.pairNext.Sub(now)
		s.pairNext = s.pairNext.Add(500 * time.Millisecond)
		s.pairMu.Unlock()
		if wait > 0 {
			time.Sleep(wait)
		}

		var q struct {
			VendorToken string `json:"vendor_token"`
		}
		if err := decode(r, &q); err != nil || strings.TrimSpace(q.VendorToken) == "" {
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprint(w, `{"error":"a vendor_token from the board's web portal is required"}`)
			return
		}

		if !s.portalAccepts(strings.TrimSpace(q.VendorToken)) {
			log.Printf("pair: rejected (portal did not accept the token) from %s", r.RemoteAddr)
			w.WriteHeader(http.StatusForbidden)
			fmt.Fprint(w, `{"error":"the board's web portal did not accept that session; sign in again and retry"}`)
			return
		}

		log.Printf("pair: issued the API key to %s", r.RemoteAddr)
		json.NewEncoder(w).Encode(map[string]string{"key": s.key})
	})

	mux.HandleFunc("/v1/screenshot", func(w http.ResponseWriter, r *http.Request) {
		if !s.auth(r) {
			http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		// Bounded: a capture that accepts the connection and then stalls (a busy
		// SoC, an HDMI mode change) would otherwise hold this goroutine for ever.
		timeout := s.captureTimeout
		if timeout <= 0 {
			timeout = 10 * time.Second
		}
		resp, err := (&http.Client{Timeout: timeout}).Get(s.captureURL)
		if err != nil {
			http.Error(w, `{"error":"capture unreachable"}`, http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()
		w.Header().Set("Content-Type", "image/jpeg")
		io.Copy(w, resp.Body)
	})

	return mux
}

func main() {
	addr := flag.String("addr", ":9200", "listen address")
	keyFile := flag.String("key-file", "/root/agentkvm/key", "file holding the API key")
	capture := flag.String("capture", "http://127.0.0.1/api/stream/snapshot", "stock firmware JPEG capture URL")
	verify := flag.String("verify-url", "http://127.0.0.1/api/vm/info", "stock firmware endpoint used by /v1/pair to validate a web-portal token")
	board := flag.String("board", "nanokvm", "gadget profile: nanokvm")
	flag.Parse()

	raw, err := os.ReadFile(*keyFile)
	key := strings.TrimSpace(string(raw))
	// Fail closed. Serving HID control unauthenticated because a key file was
	// missing is exactly the failure mode this daemon exists to not have.
	if err != nil || key == "" {
		log.Fatalf("no API key in %s: refusing to serve HID control unauthenticated (%v)", *keyFile, err)
	}

	p, ok := profiles[*board]
	if !ok {
		log.Fatalf("unknown board %q: the gadget nodes and the pointer report layout differ per board, and guessing wrong writes plausible nonsense rather than failing", *board)
	}
	h := &hid{p: p}
	// The gadget nodes may not exist yet (a firmware that builds the gadget
	// later, or rebuilds it while we run). Nothing on the board restarts a
	// daemon that has exited, and every write reopens the nodes on demand, so
	// a missing node at start is logged and served around, not fatal.
	if err := h.open(); err != nil {
		log.Printf("hid gadget: %v (serving anyway; writes retry the open)", err)
	} else if err := h.ReleaseAll(); err != nil {
		// Whatever a previous run left down is not our state. Best effort: on
		// a host that does not poll one of the interfaces this cannot
		// complete, and refusing to serve the keyboard because the pointer is
		// unread would be the wrong trade.
		log.Printf("startup release: %v (serving anyway)", err)
	}
	go h.watchdog()

	s := &server{hid: h, key: key, captureURL: *capture, verifyURL: *verify}
	log.Printf("agentkvmd listening on %s (board=%s, kbd=%s, pointer=%s)", *addr, p.name, p.kbdPath, p.mousePath)
	srv := &http.Server{
		Addr:    *addr,
		Handler: s.routes(),
		// A client that opens a connection and never finishes its headers
		// would otherwise hold a goroutine for ever. Writes get long enough
		// for a screenshot proxy that uses its whole capture timeout.
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       2 * time.Minute,
	}
	log.Fatal(srv.ListenAndServe())
}
