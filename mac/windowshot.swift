// Capture a running app's window to a PNG:
//
//     swift mac/windowshot.swift Funkuino /tmp/window.png
//
// Complements `Funkuino --snapshot` (Snapshot.swift), which renders a single
// SwiftUI screen offscreen: this one photographs the real window, including the
// web view, the traffic lights and the borderless title bar — the parts that
// only exist once the thing actually runs.
//
// Requires the calling terminal to hold macOS's Screen Recording permission
// (System Settings › Privacy & Security › Screen Recording). Without it
// screencapture silently writes a picture of the desktop instead, so the size
// check below rejects an obviously wrong result rather than handing back a
// screenshot of someone's wallpaper.

import CoreGraphics
import Foundation

let arguments = CommandLine.arguments
guard arguments.count >= 3 else {
    let usage = "usage: windowshot.swift <app name> <out.png>\n"
    FileHandle.standardError.write(Data(usage.utf8))
    exit(2)
}
let appName = arguments[1]
let output = arguments[2]

let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []

// Layer 0 is a normal document window; menu bars and panels sit above it.
let match = windows.first { window in
    guard let owner = window[kCGWindowOwnerName as String] as? String,
          owner.localizedCaseInsensitiveContains(appName),
          let layer = window[kCGWindowLayer as String] as? Int, layer == 0,
          let bounds = window[kCGWindowBounds as String] as? [String: Any],
          let height = bounds["Height"] as? Double
    else { return false }
    return height > 120   // skip tooltips and other slivers
}

guard let window = match,
      let id = window[kCGWindowNumber as String] as? Int else {
    let names = Set(windows.compactMap { $0[kCGWindowOwnerName as String] as? String })
    let message = "no window of '\(appName)' on screen. Running apps: "
        + names.sorted().joined(separator: ", ") + "\n"
    FileHandle.standardError.write(Data(message.utf8))
    exit(1)
}

let capture = Process()
capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
// -o drops the window's shadow, -x silences the shutter sound.
capture.arguments = ["-l\(id)", "-o", "-x", output]
try capture.run()
capture.waitUntilExit()
guard capture.terminationStatus == 0 else { exit(capture.terminationStatus) }

let attributes = try? FileManager.default.attributesOfItem(atPath: output)
let size = (attributes?[.size] as? Int) ?? 0
if size < 10_000 {
    let message = "captured only \(size) bytes — screen recording permission is "
        + "probably missing for this terminal.\n"
    FileHandle.standardError.write(Data(message.utf8))
    exit(1)
}
let bounds = window[kCGWindowBounds as String] as? [String: Any] ?? [:]
print("wrote \(output)  (window \(id), "
      + "\(Int(bounds["Width"] as? Double ?? 0))×\(Int(bounds["Height"] as? Double ?? 0)))")
