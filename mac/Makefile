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
CONTENTS    := $(APP)/Contents
DMG         := $(BUILD)/$(APP_NAME)-$(VERSION).dmg

SOURCES     := $(wildcard $(HERE)$(APP_NAME)/*.swift)
ICON        := $(HERE)$(APP_NAME).icns
PYTHON      := $(CODE_ROOT)/.venv/bin/python

DEV_ID         ?=
NOTARY_PROFILE ?=

.PHONY: all clean run sign notarize dmg icon

all: $(APP)

$(ICON): $(HERE)icon.py
	$(PYTHON) $(HERE)icon.py

icon: $(ICON)

# Universal from the start: an Intel Mac must not be an afterthought discovered
# at distribution time.
$(APP): $(SOURCES) $(ICON) $(MAKEFILE_LIST)
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
	@echo "built $(APP)"

run: $(APP)
	open "$(APP)"

# Every embedded binary needs its own signature, inside-out, before the bundle
# itself — this is where a bundle with a Python runtime and ffmpeg will bite.
sign: $(APP)
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
	xcrun stapler staple "$(APP)"
	spctl --assess --type execute --verbose=4 "$(APP)"

dmg: $(APP)
	rm -f "$(DMG)"
	hdiutil create -volname "$(APP_NAME)" -srcfolder "$(APP)" -ov -format UDZO "$(DMG)"
	@echo "wrote $(DMG)"

clean:
	rm -rf "$(BUILD)" "$(ICON)"
