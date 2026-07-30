// Render a view to a PNG and exit: `Funkuino --snapshot setup /tmp/setup.png`.
//
// The shell's own screens (setup, failure) are otherwise only visible to
// someone sitting in front of the Mac, which makes them the one part of this
// project that cannot be checked while changing it. Rendering them offscreen
// needs no screen-recording permission and no window, so it works over ssh, in
// a script, and for an assistant that has no eyes on the display.
//
// It does not replace looking at the real window — the web view, the traffic
// lights and the title bar are not in here (see mac/windowshot.swift for that).

import SwiftUI

enum Snapshot {
    /// Parses `--snapshot <view> <path>`; nil when not requested.
    static func requested() -> (view: String, path: String)? {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: "--snapshot"), i + 2 < args.count else { return nil }
        return (args[i + 1], args[i + 2])
    }

    @MainActor
    static func run(view name: String, to path: String) -> Never {
        let width: CGFloat = 640
        let content: AnyView

        switch name {
        case "setup":
            content = AnyView(SetupView().environmentObject(Configuration()))
        case "failure":
            content = AnyView(FailureView(
                message: "Der Server hat sich beendet.\n\nTraceback (most recent "
                       + "call last):\n  File \"studio.py\", line 1204\nOSError: "
                       + "[Errno 48] Address already in use",
                onSetup: {}))
        case "starting":
            content = AnyView(StartingView().frame(height: 320))
        default:
            FileHandle.standardError.write(
                Data("unknown view '\(name)' (setup, failure, starting)\n".utf8))
            exit(2)
        }

        let host = NSHostingView(rootView: content.frame(width: width))
        host.frame = NSRect(x: 0, y: 0, width: width,
                            height: max(host.fittingSize.height, 200))
        host.layoutSubtreeIfNeeded()

        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
            FileHandle.standardError.write(Data("cannot allocate a bitmap\n".utf8))
            exit(1)
        }
        host.cacheDisplay(in: host.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            FileHandle.standardError.write(Data("cannot encode PNG\n".utf8))
            exit(1)
        }
        do {
            try png.write(to: URL(fileURLWithPath: path))
            print("wrote \(path)  (\(Int(host.bounds.width))×\(Int(host.bounds.height)))")
        } catch {
            FileHandle.standardError.write(Data("\(error.localizedDescription)\n".utf8))
            exit(1)
        }
        exit(0)
    }
}
