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
VERSION     := 0.1.0
MIN_MACOS   := 14.0

HERE        := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
CODE_ROOT   := $(patsubst %/,%,$(dir $(patsubst %/,%,$(HERE))))
BUILD       := $(HERE)build
APP         := $(BUILD)/$(APP_NAME).app
# The bundle is a directory, and a directory's mtime does not move when files
# inside it are rewritten — making it the target let edited sources go
# unnoticed. Depend on a stamp file instead.
STAMP       := $(BUILD)/.app.stamp
CONTENTS    := $(APP)/Contents
DMG         := $(BUILD)/$(APP_NAME)-$(VERSION).dmg

SOURCES     := $(wildcard $(HERE)$(APP_NAME)/*.swift)
ICON        := $(HERE)$(APP_NAME).icns
PYTHON      := $(CODE_ROOT)/.venv/bin/python

DEV_ID         ?=
NOTARY_PROFILE ?=

.PHONY: all clean run sign notarize dmg icon

all: $(STAMP)

$(ICON): $(HERE)icon.py
	$(PYTHON) $(HERE)icon.py

icon: $(ICON)

# Universal from the start: an Intel Mac must not be an afterthought discovered
# at distribution time.
$(STAMP): $(SOURCES) $(ICON) $(MAKEFILE_LIST)
	rm -rf "$(APP)"
	mkdir -p "$(CONTENTS)/MacOS" "$(CONTENTS)/Resources"
	swiftc -O -parse-as-library -target arm64-apple-macos$(MIN_MACOS) \
	    -o "$(BUILD)/$(APP_NAME).arm64" $(SOURCES)
	swiftc -O -parse-as-library -target x86_64-apple-macos$(MIN_MACOS) \
	    -o "$(BUILD)/$(APP_NAME).x86_64" $(SOURCES)
	lipo -create -output "$(CONTENTS)/MacOS/$(APP_NAME)" \
	    "$(BUILD)/$(APP_NAME).arm64" "$(BUILD)/$(APP_NAME).x86_64"
	rm -f "$(BUILD)/$(APP_NAME).arm64" "$(BUILD)/$(APP_NAME).x86_64"
	cp "$(ICON)" "$(CONTENTS)/Resources/$(APP_NAME).icns"
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
	find "$(CONTENTS)" \( -name '*.dylib' -o -name '*.so' -o -perm -111 -type f \) \
	    ! -path "$(CONTENTS)/MacOS/$(APP_NAME)" -print0 \
	  | xargs -0 -I{} codesign --force --timestamp --options runtime \
	      --sign "$(DEV_ID)" {}
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

clean:
	rm -rf "$(BUILD)" "$(ICON)"
