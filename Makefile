# Generate deployment files dynamically to avoid hardcoding machine-local
# absolute paths in the repository.
#
# Usage:
#   make                          # generate everything (PROJECT_DIR defaults to
#                                 #   the current directory, UV auto-detected)
#   make UV=/path/to/uv           # override uv location
#   make install-systemctl-unit   # install the systemd unit (requires root)
#   make clean                    # remove generated files
#
# Generated artifacts:
#   deploy/templates/conic.service.in   systemd unit template
#   deploy/templates/update.sh.in       update script template
#   update.sh                           update script at project root (gitignored)

PROJECT_DIR := $(CURDIR)
UV ?= $(shell command -v uv 2>/dev/null || ls -t /home/*/.local/bin/uv /root/.local/bin/uv 2>/dev/null | head -1 || echo $${HOME}/.local/bin/uv)

TEMPLATES := deploy/templates
UNIT_NAME  := conic.service
UNIT_DEST  := /etc/systemd/system/$(UNIT_NAME)

.PHONY: all clean install-systemctl-unit

all: $(UNIT_NAME) update.sh

# Update script (project root, gitignored)
update.sh: $(TEMPLATES)/update.sh.in
	sed -e 's|@PROJECT_DIR@|$(PROJECT_DIR)|g' $< > $@
	chmod +x $@

# Render the systemd unit from the template
$(UNIT_NAME): $(TEMPLATES)/conic.service.in
	sed -e 's|@PROJECT_DIR@|$(PROJECT_DIR)|g' \
	    -e 's|@UV@|$(UV)|g' $< > $@

# Install the systemd unit (requires root)
install-systemctl-unit: $(UNIT_NAME)
	@if [ "$$(id -u)" -ne 0 ]; then \
		echo "Error: install-systemctl-unit must be run as root (use sudo)" >&2; \
		exit 1; \
	fi
	install -m 644 $(UNIT_NAME) $(UNIT_DEST)
	systemctl daemon-reload
	@echo "Installed $(UNIT_DEST). Enable with: systemctl enable --now $(UNIT_NAME)"

clean:
	rm -f update.sh $(UNIT_NAME)