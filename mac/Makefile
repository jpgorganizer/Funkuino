# Build Funkuino.app.
#
# Deliberately a Makefile and not an .xcodeproj: the app is one Swift file
# around the existing Python server, and a bundle is just a directory. This
# keeps the build readable, diffable and runnable from a terminal — and the
# signing/notarisation steps stay visible instead of hiding in project settings.
#
#   make            # unsigned bundle (development)
#   make sign       # + Developer ID signature       (needs DEV_ID)
#   make notarize   # + notarisation & stapling      (needs NOTARY_PROFILE)
#   make dmg        # + a distributable disk image
#
# Configuration comes from the environment or `make VAR=…`:
#   DEV_ID          "Developer ID Application: NAME (TEAMID)"
#   NOTARY_PROFILE  keychain profile from `xcrun notarytool store-credentials`

APP_NAME    := Funkuino
BUNDLE_ID   := de.sadilek.funkuino
MIN_MACOS   := 14.0
ARCH        := $(shell uname -m)

HERE        := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
CODE_ROOT   := $(patsubst %/,%,$(dir $(patsubst %/,%,$(HERE))))
# One version for the app and the Python package — pyproject.toml owns it, so a
# release cannot ship a DMG and a wheel that disagree about what they are.
VERSION     := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(CODE_ROOT)/pyproject.toml | head -1)
BUILD       := $(HERE)build
APP         := $(BUILD)/$(APP_NAME).app
# The bundle is a directory, and a directory's mtime does not move when files
# inside it are rewritten — making it the target let edited sources go
# unnoticed. Depend on a stamp file instead.
STAMP       := $(BUILD)/.app.stamp
CONTENTS    := $(APP)/Contents
# The architecture is in the name: the bundle carries a single-arch Python, so
# an Intel build is a separate file rather than a replacement.
DMG         := $(BUILD)/$(APP_NAME)-$(VERSION)-$(ARCH).dmg

SOURCES     := $(wildcard $(HERE)$(APP_NAME)/*.swift)
RUNTIME     := $(BUILD)/runtime
FFMPEG      := $(BUILD)/ffmpeg
# What the app carries of this project: the dispatcher, the scripts, and the
# instructions the embedded agent loads (its working directory is the user's
# data folder, which has no CLAUDE.md of its own).
CODE        := bin scripts requirements.txt CLAUDE.md
ICON        := $(HERE)$(APP_NAME).icns
PYTHON      := $(CODE_ROOT)/.venv/bin/python

DEV_ID         ?=
NOTARY_PROFILE ?=

.PHONY: all clean distclean run sign notarize dmg icon runtime ffmpeg release

all: $(STAMP)

$(ICON): $(HERE)icon.py
	$(PYTHON) $(HERE)icon.py

icon: $(ICON)

# ~150 MB: a pinned CPython plus this project's dependencies, so the app needs
# neither Homebrew nor a venv. Built once and kept — `make clean` leaves it.
$(RUNTIME):
	python3 $(HERE)runtime.py --out "$(RUNTIME)"

runtime: $(RUNTIME)

# ~8 MB, built from source: there is no official macOS binary, and Homebrew's is
# dynamically linked against 25 libraries and GPL-configured. See mac/ffmpeg.py.
$(FFMPEG):
	python3 $(HERE)ffmpeg.py --out "$(FFMPEG)"

ffmpeg: $(FFMPEG)

# One architecture, not universal: the embedded Python is single-architecture
# (pip has to run the interpreter it installs into), so a universal shell would
# only mean an Intel Mac launches the app and then fails at the first Python
# call. Refusing to launch is the better failure. An Intel build is the same
# `make release` run on an Intel Mac.
$(STAMP): $(SOURCES) $(ICON) $(RUNTIME) $(FFMPEG) $(MAKEFILE_LIST)
	rm -rf "$(APP)"
	mkdir -p "$(CONTENTS)/MacOS" "$(CONTENTS)/Resources"
	swiftc -O -parse-as-library -target $(ARCH)-apple-macos$(MIN_MACOS) \
	    -o "$(CONTENTS)/MacOS/$(APP_NAME)" $(SOURCES)
	cp "$(ICON)" "$(CONTENTS)/Resources/$(APP_NAME).icns"
	mkdir -p "$(CONTENTS)/Resources/code"
	cd "$(CODE_ROOT)" && rsync -a --delete --exclude '__pycache__' \
	    $(CODE) "$(CONTENTS)/Resources/code/"
	rsync -a --delete "$(RUNTIME)/" "$(CONTENTS)/Resources/code/runtime/"
	rsync -a --delete "$(FFMPEG)/" "$(CONTENTS)/Resources/ffmpeg/"
	$(PYTHON) "$(HERE)plist.py" \
	    --out "$(CONTENTS)/Info.plist" \
	    --name "$(APP_NAME)" --bundle-id "$(BUNDLE_ID)" \
	    --version "$(VERSION)" --min-macos "$(MIN_MACOS)" \
	    --code-root "$(CODE_ROOT)"
	@touch "$(STAMP)"
	@echo "built $(APP)"

run: $(STAMP)
	open "$(APP)"

# Every embedded binary needs its own signature, inside-out, before the bundle
# itself — this is where a bundle with a Python runtime and ffmpeg will bite.
sign: $(STAMP)
	@test -n "$(DEV_ID)" || { echo "DEV_ID is not set"; exit 1; }
	# Batched, not one codesign per file: the embedded runtime has hundreds of
	# .so files and every invocation costs a timestamp round-trip to Apple.
	find "$(CONTENTS)" \( -name '*.dylib' -o -name '*.so' -o -perm -111 -type f \) \
	    ! -path "$(CONTENTS)/MacOS/$(APP_NAME)" -print0 \
	  | xargs -0 codesign --force --timestamp --options runtime --sign "$(DEV_ID)"
	codesign --force --timestamp --options runtime \
	    --entitlements "$(HERE)$(APP_NAME).entitlements" \
	    --sign "$(DEV_ID)" "$(APP)"
	codesign --verify --deep --strict --verbose=2 "$(APP)"

notarize: sign
	@test -n "$(NOTARY_PROFILE)" || { echo "NOTARY_PROFILE is not set"; exit 1; }
	ditto -c -k --keepParent "$(APP)" "$(BUILD)/$(APP_NAME).zip"
	xcrun notarytool submit "$(BUILD)/$(APP_NAME).zip" \
	    --keychain-profile "$(NOTARY_PROFILE)" --wait
	for i in 1 2 3 4 5 6; do \
	    xcrun stapler staple "$(APP)" && break || { echo "ticket not ready, waiting…"; sleep 30; }; \
	done
	spctl --assess --type execute --verbose=4 "$(APP)"

dmg: $(STAMP)
	rm -f "$(DMG)"
	hdiutil create -volname "$(APP_NAME)" -srcfolder "$(APP)" -ov -format UDZO "$(DMG)"
	@echo "wrote $(DMG)"

# The whole distribution path in one go. The disk image is notarised too, not
# just the app inside it: Gatekeeper checks what the user actually downloaded.
# Stapling is retried because Apple's ticket service can lag behind its own
# "Accepted" by minutes — an unstapled build still passes, but only online.
release: notarize
	$(MAKE) dmg
	# The disk image needs its own signature: notarising an unsigned one leaves
	# Gatekeeper with "no usable signature" for the very file people download.
	codesign --force --timestamp --sign "$(DEV_ID)" "$(DMG)"
	xcrun notarytool submit "$(DMG)" --keychain-profile "$(NOTARY_PROFILE)" --wait
	for i in 1 2 3 4 5 6; do \
	    xcrun stapler staple "$(DMG)" && break || { echo "ticket not ready, waiting…"; sleep 30; }; \
	done
	xcrun stapler validate "$(DMG)"
	@echo
	@echo "Ready to hand out: $(DMG)"

# Keeps the runtime and ffmpeg: rebuilding those means downloads and a
# from-source build.
clean:
	rm -rf "$(APP)" "$(STAMP)" "$(BUILD)"/*.dmg "$(BUILD)"/*.zip "$(ICON)"

distclean:
	rm -rf "$(BUILD)" "$(ICON)"
