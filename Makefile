.PHONY: all setup network broker gateway ai status restart-all test

all: setup

setup:
	@chmod +x setup.sh scripts/*.sh
	@./setup.sh all

network:
	@./setup.sh network

broker:
	@./setup.sh broker

gateway:
	@./setup.sh gateway

ai:
	@./setup.sh ai

status:
	@python3 scripts/pi-status.py

restart-all:
	@sudo systemctl restart mosquitto avahi-daemon nginx hexapod-ai
	@python3 scripts/pi-status.py

test:
	@pytest services/ai-service/selftest.py