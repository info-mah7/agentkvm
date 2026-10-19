BIN := agentkvmd
ARCH := GOOS=linux GOARCH=riscv64 CGO_ENABLED=0

test:
	go test ./...

build: test
	$(ARCH) go build -ldflags="-s -w" -o $(BIN).riscv64 .

# Ships the binary and its init script. The key is generated on the device, so
# it never crosses this machine's disk or shell history.
install: build
	scp $(BIN).riscv64 root@$(HOST):/root/agentkvm/$(BIN)
	scp deploy/S99agentkvmd root@$(HOST):/etc/init.d/S99agentkvmd
	ssh root@$(HOST) 'chmod +x /root/agentkvm/$(BIN) /etc/init.d/S99agentkvmd && /etc/init.d/S99agentkvmd restart'

.PHONY: test build install
