// The Studio look, in AppKit terms.
//
// The setup screen appears before the web app exists, so it cannot borrow
// style.css — but it is the first thing a user sees and should not look like a
// different program. These are the same tokens, converted from the stylesheet's
// OKLCH values to sRGB, light and dark. Change one there, change it here.

import SwiftUI

enum Theme {
    private static func dynamic(light: (Double, Double, Double),
                                dark: (Double, Double, Double)) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            let (r, g, b) = isDark ? dark : light
            return NSColor(srgbRed: r, green: g, blue: b, alpha: 1)
        })
    }

    static let paper = dynamic(light: (0.9516, 0.9301, 0.9029), dark: (0.0926, 0.076, 0.0588))
    static let surface = dynamic(light: (0.9803, 0.9655, 0.9417), dark: (0.1285, 0.1071, 0.0866))
    static let surface2 = dynamic(light: (0.923, 0.8973, 0.8621), dark: (0.1737, 0.147, 0.123))
    static let ink = dynamic(light: (0.1739, 0.1502, 0.1331), dark: (0.9034, 0.8827, 0.8521))
    static let inkSoft = dynamic(light: (0.3581, 0.3308, 0.3082), dark: (0.6935, 0.6678, 0.6352))
    static let inkFaint = dynamic(light: (0.5487, 0.5233, 0.4993), dark: (0.4675, 0.4429, 0.4161))
    static let line = dynamic(light: (0.8569, 0.8301, 0.7962), dark: (0.2249, 0.1951, 0.1685))
    static let accent = dynamic(light: (0.7433, 0.3359, 0.1312), dark: (0.9049, 0.4608, 0.2271))
    static let accentInk = dynamic(light: (0.9803, 0.9655, 0.9417), dark: (0.0926, 0.076, 0.0588))
    static let warn = dynamic(light: (0.7723, 0.5561, 0.2351), dark: (0.8579, 0.6413, 0.2683))

    static let radius: CGFloat = 8

    /// The wordmark's serif, with the stylesheet's fallbacks.
    static func display(_ size: CGFloat) -> Font {
        for name in ["Iowan Old Style", "Palatino", "Georgia"] where NSFont(name: name, size: size) != nil {
            return .custom(name, size: size)
        }
        return .system(size: size, design: .serif)
    }
}

/// The page background: warm paper with the same faint accent wash the web UI
/// puts in its top corners.
struct PaperBackground: View {
    var body: some View {
        Theme.paper.overlay(alignment: .topTrailing) {
            RadialGradient(colors: [Theme.accent.opacity(0.09), .clear],
                           center: .topTrailing, startRadius: 0, endRadius: 620)
        }
        .overlay(alignment: .topLeading) {
            RadialGradient(colors: [Theme.accent.opacity(0.05), .clear],
                           center: .topLeading, startRadius: 0, endRadius: 480)
        }
        .ignoresSafeArea()
    }
}

/// Three bars, as in the header of the web app and on the app icon.
struct WaveMark: View {
    var height: CGFloat = 18

    private let ratios: [CGFloat] = [0.38, 0.9, 0.58]

    var body: some View {
        HStack(alignment: .bottom, spacing: 2.5) {
            ForEach(ratios.indices, id: \.self) { i in
                RoundedRectangle(cornerRadius: 2)
                    .fill(Theme.accent)
                    .frame(width: height * 0.19, height: height * ratios[i])
            }
        }
        .frame(height: height, alignment: .bottom)
    }
}

// MARK: - Controls

struct FunkuinoButtonStyle: ButtonStyle {
    var prominent = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13))
            .foregroundStyle(prominent ? Theme.accentInk : Theme.ink)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(
                RoundedRectangle(cornerRadius: Theme.radius)
                    .fill(prominent ? Theme.accent : Theme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.radius)
                    .stroke(prominent ? .clear : Theme.line, lineWidth: 1)
            )
            .opacity(configuration.isPressed ? 0.82 : 1)
    }
}

/// A text field that matches the web app's inputs — AppKit's own rounded style
/// carries its own grey and would stand out against the warm surface.
struct FunkuinoFieldStyle: TextFieldStyle {
    func _body(configuration: TextField<Self._Label>) -> some View {
        configuration
            .textFieldStyle(.plain)
            .font(.system(size: 13))
            .foregroundStyle(Theme.ink)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.surface))
            .overlay(RoundedRectangle(cornerRadius: Theme.radius).stroke(Theme.line, lineWidth: 1))
    }
}
