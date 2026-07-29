// Funkuino.app — a thin native shell around the existing Studio web app.
//
// The design point: Studio stays a Python aiohttp server. This app starts it as
// a child process on a free localhost port, waits for it to answer, and shows it
// in a WKWebView. Nothing of the app's logic lives here, so the shell can be
// replaced or dropped without touching the tool.
//
// Skeleton stage: the server is the one from the checkout this was built from
// (baked in as FUNKUINO_CODE_ROOT). A release build will carry its own Python
// runtime inside the bundle instead.

import SwiftUI
import WebKit

// MARK: - Server child process

/// Owns the Studio server process for the lifetime of the app.
final class StudioServer: ObservableObject {
    enum State: Equatable {
        case starting
        case running(URL)
        case failed(String)
    }

    @Published private(set) var state: State = .starting

    private var process: Process?
    private let codeRoot: URL

    init() {
        // Baked in at build time; overridable for development.
        let baked = Bundle.main.object(forInfoDictionaryKey: "FunkuinoCodeRoot") as? String
        let path = ProcessInfo.processInfo.environment["FUNKUINO_CODE_ROOT"] ?? baked ?? ""
        codeRoot = URL(fileURLWithPath: path)
    }

    func start() {
        guard process == nil else { return }
        let port: UInt16
        do {
            port = try Self.freePort()
        } catch {
            state = .failed("Kein freier Port: \(error.localizedDescription)")
            return
        }

        let dispatcher = codeRoot.appendingPathComponent("bin/funkuino")
        guard FileManager.default.isExecutableFile(atPath: dispatcher.path) else {
            state = .failed("bin/funkuino nicht gefunden unter \(codeRoot.path)")
            return
        }

        let proc = Process()
        proc.executableURL = dispatcher
        proc.arguments = ["studio", "--port", String(port), "--no-browser"]
        // A GUI app inherits a minimal PATH; give the child the usual locations
        // so it finds ffmpeg (Homebrew) until the bundle carries its own.
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        // Belt and braces against an orphaned server: stop() covers a normal
        // quit, this covers a crash, a force-quit or a signal that never reaches
        // applicationWillTerminate.
        env["FUNKUINO_EXIT_WITH_PARENT"] = "1"
        proc.environment = env

        do {
            try proc.run()
        } catch {
            state = .failed("Serverstart fehlgeschlagen: \(error.localizedDescription)")
            return
        }
        process = proc

        let url = URL(string: "http://127.0.0.1:\(port)/")!
        Task { await waitUntilAnswering(url) }
    }

    /// Poll until the server answers — it needs a moment to scan the library.
    private func waitUntilAnswering(_ url: URL) async {
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            if process?.isRunning == false {
                await MainActor.run { state = .failed("Server hat sich beendet.") }
                return
            }
            var request = URLRequest(url: url)
            request.timeoutInterval = 2
            if let (_, response) = try? await URLSession.shared.data(for: request),
               (response as? HTTPURLResponse)?.statusCode == 200 {
                await MainActor.run { state = .running(url) }
                return
            }
            try? await Task.sleep(for: .milliseconds(250))
        }
        await MainActor.run { state = .failed("Server antwortet nicht.") }
    }

    /// Must run on quit: an orphaned server would keep the port and the device
    /// websocket open.
    func stop() {
        process?.terminate()
        process = nil
    }

    /// Ask the kernel for an unused port by binding to 0 and reading it back.
    private static func freePort() throws -> UInt16 {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw POSIXError(.EADDRNOTAVAIL) }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let size = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, size) }
        }
        guard bound == 0 else { throw POSIXError(.EADDRINUSE) }
        var out = sockaddr_in()
        var outSize = size
        _ = withUnsafeMutablePointer(to: &out) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &outSize) }
        }
        return UInt16(bigEndian: out.sin_port)
    }
}

// MARK: - Web view

/// Lets the page act as the window's drag region.
///
/// With a full-size content view the WKWebView covers the whole window, the
/// transparent title bar included, and swallows the clicks that would otherwise
/// move the window. So the page reports a drag started in its header (see
/// initShell in app.js) and we hand that to AppKit.
final class DragHandler: NSObject, WKScriptMessageHandler {
    weak var webView: WKWebView?

    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard let window = webView?.window, let event = NSApp.currentEvent else { return }
        window.performDrag(with: event)
    }
}

/// Hands the live web view to the menu commands (there is no other way to reach
/// it from the App's scene, and without it Cmd-R would just beep).
final class WebViewBox: ObservableObject {
    weak var view: WKWebView?
}

struct StudioWebView: NSViewRepresentable {
    let url: URL
    let box: WebViewBox

    func makeCoordinator() -> DragHandler { DragHandler() }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.add(context.coordinator, name: "shellDrag")
        let view = WKWebView(frame: .zero, configuration: config)
        context.coordinator.webView = view
        box.view = view
        view.load(URLRequest(url: url))
        // .windowStyle(.hiddenTitleBar) alone only hides the title: the window
        // still reserves the title bar's height, which showed as a white strip
        // above the page header. Configure the window itself so the content
        // runs to the top edge and the traffic lights float over the header.
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            window.styleMask.insert(.fullSizeContentView)
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            // Without a title bar to grab, the window would be hard to move.
            window.isMovableByWindowBackground = true
        }
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {}
}

// MARK: - App

struct RootView: View {
    @EnvironmentObject var server: StudioServer
    @EnvironmentObject var web: WebViewBox

    var body: some View {
        switch server.state {
        case .starting:
            VStack(spacing: 12) {
                ProgressView()
                Text("Studio startet…").foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .running(let url):
            // ?shell=mac tells the page it is inside this window, so its header
            // can take over the role of the (hidden) title bar. ignoresSafeArea
            // is what actually lets it reach the top edge — SwiftUI insets the
            // content by the title bar height otherwise, hidden or not.
            StudioWebView(url: URL(string: "?shell=mac", relativeTo: url) ?? url, box: web)
                .ignoresSafeArea()
        case .failed(let message):
            VStack(spacing: 12) {
                Image(systemName: "exclamationmark.triangle").font(.largeTitle)
                Text(message).multilineTextAlignment(.center)
            }
            .padding(40)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var server: StudioServer?
    private var signalSources: [DispatchSourceSignal] = []

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A GUI app does not run applicationWillTerminate on SIGTERM, so a
        // `kill` would leave the server behind. Catch the signals we can.
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                self?.server?.stop()
                exit(0)
            }
            source.resume()
            signalSources.append(source)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        server?.stop()
    }
}

@main
struct FunkuinoApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var server = StudioServer()
    @StateObject private var web = WebViewBox()

    var body: some Scene {
        WindowGroup("Funkuino Studio") {
            RootView()
                .environmentObject(server)
                .environmentObject(web)
                .frame(minWidth: 900, minHeight: 620)
                .onAppear {
                    delegate.server = server
                    server.start()
                }
        }
        // No native title bar: it would show "Funkuino Studio" directly above the
        // page's own wordmark. The window's content now runs to the top edge and
        // the page header is the title bar; the traffic lights float over it.
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1180, height: 800)
        .commands {
            // Without a browser's chrome there is no reload; Cmd-R would beep.
            CommandGroup(after: .toolbar) {
                Button("Neu laden") { web.view?.reload() }
                    .keyboardShortcut("r", modifiers: .command)
            }
        }
    }
}
