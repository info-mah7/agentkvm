package main

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A temp file stands in for /dev/hidg*: the daemon only ever writes bytes to
// it, so the reports it produces are checkable without a device.
func testHid(t *testing.T) (*hid, *os.File, *os.File) {
	t.Helper()
	kbd, _ := os.CreateTemp(t.TempDir(), "hidg0")
	mouse, _ := os.CreateTemp(t.TempDir(), "hidg2")
	return &hid{p: profiles["nanokvm"], kbd: kbd, mouse: mouse}, kbd, mouse
}

func read(t *testing.T, f *os.File) []byte {
	t.Helper()
	b, err := os.ReadFile(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestKeyboardReportLayout(t *testing.T) {
	h, kbd, _ := testHid(t)
	// ctrl (0x01) + 'a' (usage 0x04)
	if err := h.Keyboard(0x01, []byte{0x04}); err != nil {
		t.Fatal(err)
	}
	want := []byte{0x01, 0, 0x04, 0, 0, 0, 0, 0} // modifier, reserved, 6 usages
	if got := read(t, kbd); !bytes.Equal(got, want) {
		t.Fatalf("keyboard report = % x, want % x", got, want)
	}
}

func TestAbsMouseIsLittleEndianAndClamped(t *testing.T) {
	h, _, mouse := testHid(t)
	if err := h.Mouse(1, 0x1234, 99999); err != nil { // y past absMax
		t.Fatal(err)
	}
	want := []byte{1, 0x34, 0x12, 0xff, 0x7f, 0} // buttons, xLo,xHi, yLo,yHi, wheel
	if got := read(t, mouse); !bytes.Equal(got, want) {
		t.Fatalf("mouse report = % x, want % x", got, want)
	}
}

// Zero is a legal coordinate. The stock firmware's LAN API rejects it, and
// silently losing the top row and left column is the kind of thing that only
// shows up as "the close button never works".
func TestZeroCoordinateIsAccepted(t *testing.T) {
	h, _, mouse := testHid(t)
	if err := h.Mouse(0, 0, 0); err != nil {
		t.Fatal(err)
	}
	want := []byte{0, 0, 0, 0, 0, 0}
	if got := read(t, mouse); !bytes.Equal(got, want) {
		t.Fatalf("mouse report = % x, want % x", got, want)
	}
}

// Releasing a button must not teleport the pointer: an absolute report always
// carries a position, so the release has to re-send the current one.
func TestReleaseAllKeepsPointerWhereItWas(t *testing.T) {
	h, _, mouse := testHid(t)
	h.Mouse(1, 1000, 2000)
	h.ReleaseAll()
	got := read(t, mouse)
	last := got[len(got)-6:]
	want := []byte{0, 0xe8, 0x03, 0xd0, 0x07, 0} // buttons 0, same 1000/2000, wheel 0
	if !bytes.Equal(last, want) {
		t.Fatalf("release report = % x, want % x", last, want)
	}
}

func TestTapIsBoundedAndReleases(t *testing.T) {
	h, kbd, _ := testHid(t)
	s := &server{hid: h, key: "secret"}
	body := `{"modifier":0,"keys":[4],"hold_ms":9999}`
	r := httptest.NewRequest(http.MethodPost, "/v1/tap", strings.NewReader(body))
	r.Header.Set("Authorization", "Bearer secret")
	w := httptest.NewRecorder()
	s.routes().ServeHTTP(w, r)

	if w.Code != 200 {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"held_ms":200`) {
		t.Fatalf("hold not capped to maxHoldMs: %s", w.Body.String())
	}
	got := read(t, kbd)
	if len(got) != 16 {
		t.Fatalf("want a press AND a release, got %d bytes", len(got))
	}
	if !bytes.Equal(got[8:], make([]byte, 8)) {
		t.Fatalf("tap did not release: % x", got[8:])
	}
}

// NanoKVM's absolute pointer is the touchpad function: no report ID, and a
// trailing wheel byte. A wrong layout moves the cursor somewhere plausible
// rather than failing, which is the worst way for this to be wrong.
func TestNanoKvmPointerLayout(t *testing.T) {
	kbd, _ := os.CreateTemp(t.TempDir(), "hidg0")
	mouse, _ := os.CreateTemp(t.TempDir(), "hidg2")
	h := &hid{p: profiles["nanokvm"], kbd: kbd, mouse: mouse}
	if err := h.Mouse(1, 0x1234, 0x0abc); err != nil {
		t.Fatal(err)
	}
	want := []byte{1, 0x34, 0x12, 0xbc, 0x0a, 0} // buttons, x LE, y LE, wheel
	if got := read(t, mouse); !bytes.Equal(got, want) {
		t.Fatalf("nanokvm pointer report = % x, want % x", got, want)
	}
	if h.p.mousePath != "/dev/hidg2" {
		t.Fatalf("nanokvm pointer must be hidg2 (hidg1 there is RELATIVE), got %s", h.p.mousePath)
	}
}

// That board has no separate wheel report, so a scroll re-sends the current
// position with the delta appended.
func TestNanoKvmWheelRidesInPointerReport(t *testing.T) {
	kbd, _ := os.CreateTemp(t.TempDir(), "hidg0")
	mouse, _ := os.CreateTemp(t.TempDir(), "hidg2")
	h := &hid{p: profiles["nanokvm"], kbd: kbd, mouse: mouse}
	h.Mouse(0, 500, 600)
	h.Wheel(-3)
	got := read(t, mouse)
	last := got[len(got)-6:]
	want := []byte{0, 0xf4, 0x01, 0x58, 0x02, 0xfd} // same 500/600, wheel -3
	if !bytes.Equal(last, want) {
		t.Fatalf("wheel report = % x, want % x", last, want)
	}
}

func TestAuthRejects(t *testing.T) {
	h, _, _ := testHid(t)
	s := &server{hid: h, key: "secret"}
	for _, hdr := range []string{"", "Bearer ", "Bearer wrong", "Bearer SECRET"} {
		r := httptest.NewRequest(http.MethodPost, "/v1/release", nil)
		if hdr != "" {
			r.Header.Set("Authorization", hdr)
		}
		w := httptest.NewRecorder()
		s.routes().ServeHTTP(w, r)
		if w.Code != http.StatusUnauthorized {
			t.Fatalf("header %q got %d, want 401", hdr, w.Code)
		}
	}
}

// Pairing hands out the key only to a caller the stock portal vouches for. A
// stand-in portal plays the part: it accepts exactly one token as a valid
// session, so the test proves the daemon asks the portal rather than trusting
// the token itself.
func TestPairTradesPortalSessionForKey(t *testing.T) {
	const good = "valid.jwt.session"

	portal := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := r.Cookie("nano-kvm-token")
		if err == nil && c.Value == good {
			w.WriteHeader(http.StatusOK)
			return
		}
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer portal.Close()

	h, _, _ := testHid(t)
	s := &server{hid: h, key: "the-real-key", verifyURL: portal.URL}

	post := func(body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodPost, "/v1/pair", strings.NewReader(body))
		w := httptest.NewRecorder()
		s.routes().ServeHTTP(w, r)
		return w
	}

	// A valid session gets the key, with no bearer key presented.
	w := post(`{"vendor_token":"` + good + `"}`)
	if w.Code != http.StatusOK {
		t.Fatalf("valid token got %d, want 200 (%s)", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "the-real-key") {
		t.Fatalf("valid token did not return the key: %s", w.Body.String())
	}

	// A token the portal rejects gets nothing, and the key never appears.
	w = post(`{"vendor_token":"forged"}`)
	if w.Code != http.StatusForbidden {
		t.Fatalf("bad token got %d, want 403", w.Code)
	}
	if strings.Contains(w.Body.String(), "the-real-key") {
		t.Fatal("the key leaked on a rejected pairing")
	}

	// An empty request is a bad request, not a 403 and not a key.
	if w := post(`{}`); w.Code != http.StatusBadRequest {
		t.Fatalf("empty token got %d, want 400", w.Code)
	}

	// GET is not pairing.
	r := httptest.NewRequest(http.MethodGet, "/v1/pair", nil)
	w = httptest.NewRecorder()
	s.routes().ServeHTTP(w, r)
	if w.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET got %d, want 405", w.Code)
	}
}

// A /v1/keys report sent while a tap is holding its key must wait for the
// tap's release. Without the sequence lock it landed in the hold: the tap's
// release then let go of the other caller's key.
func TestTapIsNotInterleaved(t *testing.T) {
	h, kbd, _ := testHid(t)
	s := &server{hid: h, key: "secret"}
	post := func(path, body string) {
		r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer secret")
		w := httptest.NewRecorder()
		s.routes().ServeHTTP(w, r)
		if w.Code != 200 {
			t.Errorf("%s: status %d: %s", path, w.Code, w.Body.String())
		}
	}

	done := make(chan struct{})
	go func() { post("/v1/tap", `{"modifier":0,"keys":[4],"hold_ms":150}`); close(done) }()
	time.Sleep(40 * time.Millisecond) // well inside the tap's hold
	post("/v1/keys", `{"modifier":0,"keys":[5]}`)
	<-done

	got := read(t, kbd)
	if len(got) != 24 {
		t.Fatalf("want press, release, then the keys report: got %d bytes", len(got))
	}
	if got[2] != 4 || !bytes.Equal(got[8:16], make([]byte, 8)) || got[16+2] != 5 {
		t.Fatalf("keys report interleaved into the tap: % x", got)
	}
}

// A capture source that accepts the connection and then stalls must not hold
// /v1/screenshot for ever.
func TestScreenshotTimesOut(t *testing.T) {
	stall := make(chan struct{})
	capture := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-stall
	}))
	defer capture.Close()
	defer close(stall)

	h, _, _ := testHid(t)
	s := &server{hid: h, key: "secret", captureURL: capture.URL, captureTimeout: 150 * time.Millisecond}
	r := httptest.NewRequest(http.MethodGet, "/v1/screenshot", nil)
	r.Header.Set("Authorization", "Bearer secret")
	w := httptest.NewRecorder()

	start := time.Now()
	s.routes().ServeHTTP(w, r)
	if took := time.Since(start); took > 2*time.Second {
		t.Fatalf("screenshot waited %v on a stalled capture", took)
	}
	if w.Code != http.StatusBadGateway {
		t.Fatalf("want 502 from a stalled capture, got %d", w.Code)
	}
}

// Concurrent pairing attempts must queue behind one another: the throttle is
// a cumulative slot, not "wait up to 500 ms and then all arrive together".
func TestPairThrottleIsCumulative(t *testing.T) {
	portal := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer portal.Close()
	h, _, _ := testHid(t)
	s := &server{hid: h, key: "k", verifyURL: portal.URL}
	mux := s.routes()

	start := time.Now()
	done := make(chan struct{})
	for i := 0; i < 3; i++ {
		go func() {
			r := httptest.NewRequest(http.MethodPost, "/v1/pair", strings.NewReader(`{"vendor_token":"x"}`))
			mux.ServeHTTP(httptest.NewRecorder(), r)
			done <- struct{}{}
		}()
	}
	for i := 0; i < 3; i++ {
		<-done
	}
	// First at 0, second at 500 ms, third at 1000 ms.
	if el := time.Since(start); el < 900*time.Millisecond {
		t.Fatalf("three concurrent attempts finished in %v; the throttle let them through together", el)
	}
}

// A write the kernel holds (the NanoKVM's hidg driver retrying a wakeup the
// sleeping target refuses) must not hold hid.mu: one stuck mouse report used
// to keep every later key waiting for good. A full pipe in blocking mode
// stands in, since its write blocks in the kernel too, out of the poller's
// reach.
func TestStuckWriteDoesNotBlockTheOtherInterface(t *testing.T) {
	var p [2]int
	if err := syscall.Pipe(p[:]); err != nil {
		t.Fatal(err)
	}
	syscall.SetNonblock(p[1], true)
	for {
		if _, err := syscall.Write(p[1], make([]byte, 4096)); err != nil {
			break
		}
	}
	syscall.SetNonblock(p[1], false)
	mouse := os.NewFile(uintptr(p[1]), "hidg2")
	drain := os.NewFile(uintptr(p[0]), "drain")
	defer drain.Close()
	h, kbd, _ := testHid(t)
	h.mouse = mouse

	start := time.Now()
	if err := h.Mouse(0, 10, 10); !errors.Is(err, ErrNotPolled) {
		t.Fatalf("stuck pointer write: want ErrNotPolled, got %v", err)
	}
	if err := h.Keyboard(0, []byte{4}); err != nil {
		t.Fatalf("keyboard behind a stuck pointer: %v", err)
	}
	if err := h.Mouse(0, 20, 20); !errors.Is(err, ErrNotPolled) {
		t.Fatalf("second pointer write while stuck: want ErrNotPolled, got %v", err)
	}
	if took := time.Since(start); took > 2*(reportTimeout+stuckGrace) {
		t.Fatalf("took %s: the stuck write held the lock", took)
	}
	if got := read(t, kbd); len(got) != 8 || got[2] != 4 {
		t.Fatalf("keyboard report did not go out: % x", got)
	}

	// The target wakes: the write returns, the file is usable again, and
	// what the late report pressed is left for the watchdog to release.
	go io.Copy(io.Discard, drain)
	deadline := time.Now().Add(2 * time.Second)
	for {
		h.mu.Lock()
		stuck, held, last := h.stuck[mouse], h.held, h.lastAct
		h.mu.Unlock()
		if !stuck {
			if !held || !last.IsZero() {
				t.Fatalf("after the late write: held=%v lastAct=%v, want held and zero", held, last)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the stuck write never cleared")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := h.Mouse(0, 30, 30); err != nil {
		t.Fatalf("pointer after waking: %v", err)
	}
}
