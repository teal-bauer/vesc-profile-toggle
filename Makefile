PYTHON ?= python3
NAME   := vesc-profile-toggle
VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo 0.0.0-dev)
BUNDLE := dist/$(NAME)-$(VERSION)
ARTIFACT := dist/$(NAME)-$(VERSION).tar.gz

.PHONY: test bundle clean

test:
	$(PYTHON) -m unittest discover -s tests -v

# Whatever lands under data/ is the exact /data/extensions layout used on the
# MDB. The example preset packets are replaced at install time with the
# controller's own presets.
bundle: test
	rm -rf $(BUNDLE) $(ARTIFACT)
	mkdir -p $(BUNDLE)/data/extensions/bin $(BUNDLE)/data/extensions/vesc-profiles
	install -m 755 bin/vesc-profile-toggle $(BUNDLE)/data/extensions/bin/vesc-profile-toggle
	install -m 700 src/vesc_profile_io.py $(BUNDLE)/data/extensions/bin/vesc_profile_io.py
	install -m 700 src/vesc_profile_toggle.py $(BUNDLE)/data/extensions/bin/vesc_profile_toggle.py
	install -m 644 rules/vesc-profile-toggle.toml $(BUNDLE)/data/extensions/vesc-profile-toggle.toml
	install -m 600 fixtures/default.frames $(BUNDLE)/data/extensions/vesc-profiles/default.frames
	install -m 600 fixtures/limited.frames $(BUNDLE)/data/extensions/vesc-profiles/limited.frames
	cp README.md $(BUNDLE)/README.md
	cd $(BUNDLE) && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
	tar -C dist -czf $(ARTIFACT) $(NAME)-$(VERSION)
	@echo "built $(ARTIFACT)"

clean:
	rm -rf dist
