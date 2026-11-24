# Install the dps150ctl command into an isolated environment on your PATH
# (~/.local/bin by default). Uses uv when available, otherwise pipx.
UV ?= uv
PIPX ?= pipx
PYTHON ?= .venv/bin/python

.PHONY: install uninstall test lint

install:
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) tool install --force --reinstall . ; \
	elif command -v $(PIPX) >/dev/null 2>&1; then \
		$(PIPX) install --force . ; \
	else \
		echo "install needs uv or pipx" >&2; exit 1; \
	fi
	@command -v dps150ctl >/dev/null 2>&1 || echo "note: dps150ctl's bin directory is not on PATH" >&2

uninstall:
	@if command -v $(UV) >/dev/null 2>&1 && $(UV) tool list 2>/dev/null | grep -q '^dps150ctl '; then \
		$(UV) tool uninstall dps150ctl; \
	elif command -v $(PIPX) >/dev/null 2>&1; then \
		$(PIPX) uninstall dps150ctl; \
	fi

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check .
