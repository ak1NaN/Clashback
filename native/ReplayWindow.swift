import AppKit
import Foundation
import IOSurface
import QuartzCore
import Darwin
import UniformTypeIdentifiers
import CoreText

func isHTMLDocument(_ url: URL) -> Bool {
    ["html", "htm"].contains(url.pathExtension.lowercased())
}

final class HTMLFilePickerFilter: NSObject, NSOpenSavePanelDelegate {
    func panel(_ sender: Any, shouldEnable url: URL) -> Bool {
        // Let the user browse folders, and accept saved webpages even when
        // Launch Services classifies an .htm file as plain text or an unknown UTI.
        let directory = (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
        return directory || isHTMLDocument(url)
    }

    func panel(_ sender: Any, validate url: URL) throws {
        guard isHTMLDocument(url) else {
            throw NSError(domain: "ReplayWindow.HTMLFilePicker", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "请选择 .html 或 .htm 网页文件。"])
        }
    }
}

// NSOpenPanel retains its delegate weakly, so keep the shared filter alive.
let htmlFilePickerFilter = HTMLFilePickerFilter()

func htmlFilePicker() -> NSOpenPanel {
    let panel = NSOpenPanel()
    // Filename filtering is independent of macOS's per-file content type cache.
    panel.allowedContentTypes = []
    panel.allowsOtherFileTypes = true
    panel.delegate = htmlFilePickerFilter
    panel.canChooseFiles = true
    panel.canChooseDirectories = false
    panel.allowsMultipleSelection = false
    panel.message = "选择保存的 RoyaleAPI 网页（.html 或 .htm）"
    return panel
}

final class ReplayButton: NSButton {
    // The first click must open the picker even if the newly launched window
    // has not received activation yet, or another application is in front.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

func projectRoot(for folder: URL) -> URL? {
    var current = folder
    while current.path != "/" {
        if FileManager.default.fileExists(atPath: current.appendingPathComponent("studio.py").path) { return current }
        current.deleteLastPathComponent()
    }
    return nil
}

// Lightweight loading UI, using the game's font and the 720×1600 stock
// loading-screen layout. Native battle rendering is independent of these views.
// Verified in loading.sc: hint text ID70 uses Supercell-Magic size16; loading
// text ID73 uses the same font size32. Screen sizes below retain that 1:2 ratio
// at a reference 1.5 scale; positions are calibrated to the stock screenshot.
func drawLoadingText(_ text: String, font: NSFont, center: NSPoint, width: CGFloat) {
    guard !text.isEmpty, let context = NSGraphicsContext.current?.cgContext else { return }
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    paragraph.lineBreakMode = .byWordWrapping
    let fill: [NSAttributedString.Key: Any] = [
        .font: font, .foregroundColor: NSColor.white, .paragraphStyle: paragraph]
    var outline = fill
    outline[.foregroundColor] = NSColor.black
    outline[.strokeColor] = NSColor.black
    outline[.strokeWidth] = -12
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: fill))
    // AppKit may leave a previous text position on this CGContext. Measuring
    // with it shifts every new label; nil measures glyphs at their own origin.
    let ink = CTLineGetImageBounds(line, nil)
    if ink.width <= width && !text.contains("\n") {
        // Center visible glyphs, rather than the font's extra ascender/descender
        // padding. This keeps the percentage crossing the top of the bar.
        context.saveGState()
        context.textMatrix = .identity
        let origin = NSPoint(x: center.x-ink.midX, y: center.y-ink.midY)
        context.textPosition = NSPoint(x: origin.x, y: origin.y-font.pointSize*0.035)
        CTLineDraw(CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: outline)), context)
        context.textPosition = origin
        CTLineDraw(line, context)
        context.restoreGState()
    } else {
        let size = (text as NSString).boundingRect(with: NSSize(width: width, height: 1000),
            options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: fill).size
        let rect = NSRect(x: center.x-width/2, y: center.y-size.height/2, width: width, height: size.height)
        (text as NSString).draw(with: rect.offsetBy(dx: 0, dy: -font.pointSize*0.035),
            options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: outline)
        (text as NSString).draw(with: rect, options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: fill)
    }
}

final class LoadingTip: NSView {
    var text = ""
    var fontName: String?
    var uiScale: CGFloat = 1
    override var isOpaque: Bool { false }
    override func draw(_ dirtyRect: NSRect) {
        let font = fontName.flatMap { NSFont(name: $0, size: 24*uiScale) }
            ?? NSFont.systemFont(ofSize: 24*uiScale, weight: .heavy)
        drawLoadingText(text, font: font, center: NSPoint(x: bounds.midX, y: bounds.midY), width: bounds.width-8)
    }
}

final class LoadingProgressBar: NSView {
    var fraction = 0.0
    var fontName: String?
    var uiScale: CGFloat = 1
    override var isOpaque: Bool { false }

    func loadFont(project: URL?) {
        guard let fontFolder = project?.appendingPathComponent("game/assets/font") else { return }
        for name in ["supercell-magic_0.ttf", "sc_ccbackbeatregular.ttf"] {
            let file = fontFolder.appendingPathComponent(name)
            guard let descriptors = CTFontManagerCreateFontDescriptorsFromURL(file as CFURL) as? [CTFontDescriptor],
                  let descriptor = descriptors.first,
                  let name = CTFontDescriptorCopyAttribute(descriptor, kCTFontNameAttribute) as? String else { continue }
            CTFontManagerRegisterFontsForURL(file as CFURL, .process, nil)
            fontName = name
            break
        }
    }

    func update(_ object: [String: Any]) {
        if let value = object["fraction"] as? NSNumber, value.doubleValue.isFinite {
            fraction = min(1, max(0, value.doubleValue))
        } else { fraction = 0 }
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        let s = uiScale
        let outer = NSRect(x: 0, y: 0, width: bounds.width, height: 56*s)
        NSColor.black.setFill(); outer.fill()
        let track = outer.insetBy(dx: 0, dy: 5*s)
        NSColor(calibratedRed: 0.04, green: 0.12, blue: 0.23, alpha: 1).setFill(); track.fill()
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(rect: track).addClip()
        let filled = NSRect(x: track.minX, y: track.minY, width: track.width*CGFloat(fraction), height: track.height)
        let blue = NSGradient(colorsAndLocations:
            (NSColor(calibratedRed: 0.04, green: 0.35, blue: 0.68, alpha: 1), 0),
            (NSColor(calibratedRed: 0.08, green: 0.45, blue: 0.83, alpha: 1), 0.56),
            (NSColor(calibratedRed: 0.39, green: 0.70, blue: 0.98, alpha: 1), 0.57),
            (NSColor(calibratedRed: 0.29, green: 0.61, blue: 0.94, alpha: 1), 1))!
        if filled.width > 0 {
            blue.draw(in: filled, angle: 90)
            NSColor(calibratedRed: 0.54, green: 0.82, blue: 1, alpha: 1).setFill()
            NSRect(x: filled.minX, y: filled.maxY-2*s, width: filled.width, height: 2*s).fill()
        }
        NSGraphicsContext.restoreGraphicsState()
        let text = "\(Int((fraction*100).rounded()))%"
        let font = fontName.flatMap { NSFont(name: $0, size: 48*s) }
            ?? NSFont.systemFont(ofSize: 48*s, weight: .heavy)
        drawLoadingText(text, font: font, center: NSPoint(x: bounds.midX, y: 48*s), width: bounds.width-8)
    }
}

// Tiny display only: the game draws the pixels. No battle logic or sprites here.
final class TimelineSlider: NSSlider {
    var tracking = false
    override func mouseDown(with event: NSEvent) {
        tracking = true; super.mouseDown(with: event); tracking = false
        window?.makeFirstResponder(superview)
    }
}

final class ReplayView: NSView {
    let folder: URL
    var image: CGImage?
    var number: UInt32 = 0
    var paused = false
    var speed: Double = 1
    var requestedSpeed: Double?
    let speedSteps: [Double] = [0.5, 1, 2, 4, 8]
    var steps = 0
    var seekID = 0
    var tick: Int32 = -1
    var status = "正在准备本地对局…"
    var timer: Timer?
    var shared: UnsafeMutableRawPointer?
    var sharedSize = 0
    var lastSeq: UInt32 = 0
    var surfaces: [IOSurfaceRef] = []
    var surfaceIds: [UInt32] = []
    let pixelsLayer = CALayer()
    var mode = "RGBA"
    var sampleTime = ProcessInfo.processInfo.systemUptime
    var sampleFrame: UInt32 = 0
    var sampleTick: Int32 = -1
    var presented = 0
    var fps: Double = 0
    var actualSpeed: Double = 0
    var loading = true
    var showNativeLoading = false
    var loadingBackground: NSImage?
    var loadingBackgroundPath = ""
    var loadingTip = "正在准备本地对局…"
    let loadingLabel = LoadingTip()
    let loadingProgress = LoadingProgressBar()
    var waitingForHTML = false
    let timeline = TimelineSlider(value: 2, minValue: 2, maxValue: 7200, target: nil, action: nil)
    let timeLabel = NSTextField(labelWithString: "0:00 / 0:00")
    var buttons: [NSButton] = []
    var htmlPanel: NSOpenPanel?
    var endTick = 7200
    var seeking = false
    var progressRead = 0.0
    var reconstructionNotice = ""
    var recordingMode = false
    var panelHeight: CGFloat { recordingMode ? 0 : 116 }
    override var acceptsFirstResponder: Bool { true }

    init(folder: URL) {
        self.folder = folder
        super.init(frame: NSRect(x: 0, y: 0, width: 450, height: 1000))
        if let control = try? String(contentsOf: folder.appendingPathComponent("control.txt"), encoding: .utf8) {
            for item in control.split(whereSeparator: { $0.isWhitespace }) {
                let pair = item.split(separator: "=", maxSplits: 1)
                if pair.count == 2 && pair[0] == "speed", let initial = Double(pair[1]) { speed = initial }
                if pair.count == 2 && pair[0] == "paused" { paused = pair[1] == "1" }
            }
        }
        let settings = folder.deletingLastPathComponent().appendingPathComponent("settings.json")
        if let bytes = try? Data(contentsOf: settings),
           let options = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] {
            showNativeLoading = options["show_native_loading"] as? Bool ?? false
            recordingMode = options["recording_mode"] as? Bool ?? false
            loadingTip = options["loading_tip"] as? String ?? loadingTip
            if let path = options["loading_background"] as? String, !path.isEmpty {
                loadingBackgroundPath = path
                loadingBackground = NSImage(contentsOfFile: path)
            }
        }
        wantsLayer = true
        pixelsLayer.contentsGravity = .resizeAspect
        pixelsLayer.actions = ["contents": NSNull(), "bounds": NSNull(), "position": NSNull()]
        layer?.addSublayer(pixelsLayer)
        pixelsLayer.isHidden = true
        loadingProgress.loadFont(project: projectRoot(for: folder))
        loadingLabel.text = loadingTip; loadingLabel.fontName = loadingProgress.fontName
        loadingLabel.wantsLayer = true; loadingLabel.layer?.zPosition = 11
        addSubview(loadingLabel)
        loadingProgress.wantsLayer = true; loadingProgress.layer?.zPosition = 11
        addSubview(loadingProgress)
        if let data = try? Data(contentsOf: folder.deletingLastPathComponent().appendingPathComponent("progress.json")),
           let initial = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            waitingForHTML = (initial["phase"] as? String) == "idle"
            loadingProgress.isHidden = waitingForHTML
            loadingProgress.update(initial)
            status = initial["message"] as? String ?? status
        }
        timeline.target = self; timeline.action = #selector(slideTime)
        timeline.isContinuous = false; timeline.isEnabled = false
        addSubview(timeline)
        timeLabel.textColor = .white; timeLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular)
        addSubview(timeLabel)
        for (title, action) in [("打开 HTML…", #selector(openOther)), ("播放/暂停", #selector(togglePause)), ("重播", #selector(restartReplay))] {
            let b = ReplayButton(title: title, target: self, action: action)
            b.bezelStyle = .rounded; b.font = .systemFont(ofSize: 10)
            b.refusesFirstResponder = true; addSubview(b); buttons.append(b)
        }
        for b in buttons.dropFirst() { b.isEnabled = false }
        if recordingMode {
            timeline.isHidden = true; timeLabel.isHidden = true
            for b in buttons { b.isHidden = true }
        }
        let bgPixels = loadingBackground?.cgImage(forProposedRect: nil, context: nil, hints: nil)
        let backgroundState: [String: Any] = ["path": loadingBackgroundPath,
            "loaded": loadingBackground != nil,
            "width": bgPixels?.width ?? 0, "height": bgPixels?.height ?? 0]
        if let data = try? JSONSerialization.data(withJSONObject: backgroundState) {
            try? data.write(to: folder.appendingPathComponent("background.status.json"))
        }
        timer = Timer(timeInterval: 1.0 / 120.0, repeats: true) { [weak self] _ in self?.readFrame() }
        RunLoop.main.add(timer!, forMode: .common)
    }
    required init?(coder: NSCoder) { fatalError() }
    deinit { if let shared = shared { munmap(shared, sharedSize) } }
    override func layout() {
        super.layout()
        CATransaction.begin(); CATransaction.setDisableActions(true)
        pixelsLayer.frame = NSRect(x: 0, y: panelHeight, width: bounds.width, height: max(1, bounds.height-panelHeight))
        // Stock reference: 56 px bar, percentage centered 48 px from the
        // bottom, tip centered 146 px from the bottom at 720×1600.
        // Bound scale on very wide windows so the loading UI stays proportional.
        let loadingHeight = max(1, bounds.height-panelHeight)
        let uiScale = max(0.3, min(bounds.width/720, loadingHeight/900))
        loadingProgress.uiScale = uiScale
        loadingProgress.frame = NSRect(x: 0, y: panelHeight, width: bounds.width, height: 82*uiScale)
        loadingLabel.uiScale = uiScale
        loadingLabel.frame = NSRect(x: 28*uiScale, y: panelHeight+106*uiScale,
            width: max(1, bounds.width-56*uiScale), height: 80*uiScale)
        loadingProgress.needsDisplay = true; loadingLabel.needsDisplay = true
        timeline.frame = NSRect(x: 8, y: 77, width: max(1, bounds.width-16), height: 20)
        timeLabel.frame = NSRect(x: 10, y: 99, width: max(1, bounds.width-20), height: 14)
        let bw = max(36, (bounds.width-12)/CGFloat(buttons.count))
        for (i, button) in buttons.enumerated() { button.frame = NSRect(x: 6+CGFloat(i)*bw, y: 40, width: bw, height: 26) }
        CATransaction.commit()
        needsDisplay = true
    }
    func u32(_ d: Data, _ offset: Int) -> UInt32 {
        UInt32(d[offset]) | UInt32(d[offset+1]) << 8 | UInt32(d[offset+2]) << 16 | UInt32(d[offset+3]) << 24
    }
    func readFrame() {
        let now = ProcessInfo.processInfo.systemUptime
        if now-progressRead > 0.2 {
            progressRead = now
            let progress = folder.deletingLastPathComponent().appendingPathComponent("progress.json")
            if let bytes = try? Data(contentsOf: progress),
               let obj = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] {
                reconstructionNotice = obj["notice"] as? String ?? ""
                if loading, let message = obj["message"] as? String { status = message; needsDisplay = true }
                if loading {
                    loadingProgress.update(obj)
                    waitingForHTML = (obj["phase"] as? String) == "idle"
                    loadingProgress.isHidden = waitingForHTML
                }
            }
            if let requested = ProcessInfo.processInfo.environment["NEO_CAPTURE_LOADING"], loading,
               !FileManager.default.fileExists(atPath: requested),
               let bitmap = bitmapImageRepForCachingDisplay(in: bounds) {
                cacheDisplay(in: bounds, to: bitmap)
                if let data = bitmap.representation(using: .png, properties: [:]) { try? data.write(to: URL(fileURLWithPath: requested)) }
            }
        }
        let path = folder.appendingPathComponent("frame.shm")
        if shared == nil {
            let fd = Darwin.open(path.path, O_RDONLY)
            if fd >= 0 {
                var st = stat()
                if fstat(fd, &st) == 0 && st.st_size > 64,
                   let mapped = mmap(nil, Int(st.st_size), PROT_READ, MAP_SHARED, fd, 0), mapped != MAP_FAILED {
                    shared = mapped; sharedSize = Int(st.st_size)
                }
                Darwin.close(fd)
            }
        }
        guard let shared = shared else { return }
        let seqBefore = shared.load(fromByteOffset: 32, as: UInt32.self)
        guard seqBefore % 2 == 0, seqBefore != lastSeq else { return }
        let data = Data(bytes: shared, count: 64)
        guard data.prefix(4) == Data("CRXF".utf8) else { return }
        let seq = u32(data, 32)
        guard seq == seqBefore else { return }
        let frame = u32(data, 4)
        guard frame != number else { return }
        let w = Int(u32(data, 8)), h = Int(u32(data, 12))
        guard w > 0, h > 0, w <= 4096, h <= 4096, sharedSize == 64 + w*h*4 else { return }
        let nextTick = Int32(bitPattern: u32(data, 16))
        let isLoading = nextTick < 0 && !showNativeLoading
        if loading != isLoading { loading = isLoading; needsDisplay = true }
        pixelsLayer.isHidden = isLoading
        loadingLabel.isHidden = !isLoading
        loadingProgress.isHidden = !isLoading || waitingForHTML
        let ids = [u32(data, 52), u32(data, 56)]
        if ids.allSatisfy({ $0 != 0 }) {
            if surfaceIds != ids || surfaces.count != 2 {
                surfaces = ids.compactMap { IOSurfaceLookup($0) }; surfaceIds = ids
            }
            let index = Int(u32(data, 60))
            guard surfaces.count == 2, index < 2,
                  shared.load(fromByteOffset: 32, as: UInt32.self) == seq else { return }
            mode = "IOSurface"
            CATransaction.begin(); CATransaction.setDisableActions(true)
            pixelsLayer.contents = surfaces[index]
            CATransaction.commit()
        } else {
            let pixels = Data(bytes: shared.advanced(by: 64), count: w*h*4)
            guard shared.load(fromByteOffset: 32, as: UInt32.self) == seq,
                  let provider = CGDataProvider(data: pixels as CFData),
                  let cg = CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w*4,
                               space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.last.rawValue),
                               provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent) else { return }
            mode = "RGBA"; image = cg; pixelsLayer.contents = nil
            needsDisplay = true
        }
        lastSeq = seq
        number = frame
        presented += 1
        if recordingMode {
            // ScreenCaptureKit needs window damage notifications even when
            // all controls are hidden and an IOSurface pointer is reused.
            needsDisplay = true
            displayIfNeeded()
            CATransaction.flush()
        }
        tick = Int32(bitPattern: u32(data, 16))
        let end = Int32(bitPattern: u32(data, 36))
        if end > 0 { endTick = Int(end); timeline.maxValue = Double(end) }
        seeking = u32(data, 40) != 0
        timeline.isEnabled = tick >= 2 && !seeking
        if !timeline.tracking { timeline.doubleValue = Double(max(2, tick)) }
        for b in buttons.dropFirst() { b.isEnabled = tick >= 2 && !seeking }
        let current = max(0, Int(tick))/20
        paused = u32(data, 20) != 0
        let reportedSpeed = Double(Float(bitPattern: u32(data, 24)))
        if let requested = requestedSpeed, abs(reportedSpeed-requested) < 0.001 { requestedSpeed = nil }
        speed = requestedSpeed ?? reportedSpeed
        timeLabel.stringValue = String(format: "%d:%02d / %d:%02d · %.1f×%@", current/60, current%60, endTick/1200, (endTick/20)%60, speed, seeking ? " · 正在跳转…" : "")
        let elapsed = now-sampleTime
        if elapsed >= 2 {
            fps = Double(presented)/elapsed
            let tps = tick >= sampleTick && sampleTick >= 0 ? Double(tick-sampleTick)/elapsed : 0
            actualSpeed = tps/20
            let stats: [String: Any] = ["mode": mode, "presented_fps": fps,
                "producer_fps": Double(frame-sampleFrame)/elapsed,
                "ticks_per_second": tps, "actual_speed": actualSpeed,
                "tick": tick, "paused": paused, "speed": speed, "sample_seconds": elapsed]
            if let encoded = try? JSONSerialization.data(withJSONObject: stats, options: [.prettyPrinted, .sortedKeys]) {
                try? encoded.write(to: folder.appendingPathComponent("viewer.stats.json"), options: .atomic)
            }
            sampleTime = now; sampleFrame = frame; sampleTick = tick; presented = 0
        }
        let text = String(format: "%d fps · 实际 %.1f× · 设置 %.1f×%@", Int(fps.rounded()), actualSpeed, speed, paused ? " · 暂停" : "") +
            "\n" + (reconstructionNotice.isEmpty ? "←/→ ±1秒 · ↑/↓ 调速 0.5–8× · 空格暂停" : reconstructionNotice)
        // Redraw only the small status text periodically; IOSurface owns the pixels.
        if !loading && text != status && (frame % 15 == 0 || paused) {
            status = text; needsDisplay = true
        }
    }
    @objc func openOther() {
        if let existing = htmlPanel { existing.makeKeyAndOrderFront(nil); return }
        guard let window = window else { return }
        let panel = htmlFilePicker()
        htmlPanel = panel
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        // Keep this panel attached to the viewer and leave its event loop free
        // while the independent engine process is still preparing a battle.
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self = self else { return }
            if response == .OK, let url = panel.url {
                try? url.path.write(to: self.folder.appendingPathComponent("open-html.txt"), atomically: true, encoding: .utf8)
            }
            self.htmlPanel = nil
            self.window?.makeFirstResponder(self)
        }
    }
    func jump(_ target: Int) { paused = true; control(seek: min(endTick, max(2, target))); window?.makeFirstResponder(self) }
    @objc func slideTime() { jump(timeline.integerValue) }
    @objc func restartReplay() { jump(2) }
    @objc func togglePause() { paused.toggle(); control(); window?.makeFirstResponder(self) }
    func setSpeed(_ value: Double) {
        speed = min(8, max(0.5, value)); requestedSpeed = speed; control()
        needsDisplay = true
    }
    func adjustSpeed(_ direction: Int) {
        if direction > 0 { setSpeed(speedSteps.first(where: { $0 > speed+0.001 }) ?? 8) }
        else { setSpeed(speedSteps.last(where: { $0 < speed-0.001 }) ?? 0.5) }
    }
    func control(seek: Int? = nil) {
        var text = "paused=\(paused ? 1 : 0) speed=\(speed) step=\(steps) mark=0"
        if let target = seek { seekID += 1; text += " seek=\(target) seek_id=\(seekID)" }
        else { text += " seek_id=\(seekID)" }
        try? text.write(to: folder.appendingPathComponent("control.txt"), atomically: true, encoding: .utf8)
    }
    override func keyDown(with event: NSEvent) {
        switch event.keyCode {
        case 123, 124:
            if tick >= 2 && !seeking { jump(Int(tick) + (event.keyCode == 123 ? -20 : 20)) }
            return
        case 126: adjustSpeed(1); return
        case 125: adjustSpeed(-1); return
        default: break
        }
        switch event.charactersIgnoringModifiers?.lowercased() {
        case " ": paused.toggle(); control()
        case "1": setSpeed(0.5)
        case "2": setSpeed(1)
        case "3": setSpeed(2)
        case "4": setSpeed(4)
        case "5": setSpeed(8)
        // Native intro skipping starts the rendered battle at tick 2. Asking
        // upstream for tick 0 leaves its seek state waiting indefinitely.
        case "r": paused = true; control(seek: 2)
        case "q": NSApp.terminate(nil)
        default: super.keyDown(with: event)
        }
    }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.setFill(); bounds.fill()
        if loading, let background = loadingBackground, background.size.width > 0, background.size.height > 0 {
            // Cover the entire window without distortion; crop excess edges.
            let scale = max(bounds.width / background.size.width, bounds.height / background.size.height)
            let size = NSSize(width: background.size.width*scale, height: background.size.height*scale)
            NSGraphicsContext.saveGraphicsState()
            NSBezierPath(rect: bounds).addClip()
            background.draw(in: NSRect(x: bounds.midX-size.width/2, y: bounds.midY-size.height/2,
                                       width: size.width, height: size.height),
                            from: .zero, operation: .sourceOver, fraction: 1)
            NSGraphicsContext.restoreGraphicsState()
            NSColor.black.withAlphaComponent(0.70).setFill()
            NSRect(x: 0, y: 0, width: bounds.width, height: panelHeight).fill()
        }
        if !loading, let image = image, let ctx = NSGraphicsContext.current?.cgContext {
            let available = NSRect(x: 0, y: panelHeight, width: bounds.width, height: max(1, bounds.height - panelHeight))
            let scale = min(available.width / CGFloat(image.width), available.height / CGFloat(image.height))
            let w = CGFloat(image.width)*scale, h = CGFloat(image.height)*scale
            // glReadPixels is bottom-up; CGImage row zero is drawn at the top, so flip vertically.
            ctx.saveGState()
            ctx.translateBy(x: (bounds.width-w)/2, y: available.minY + (available.height+h)/2)
            ctx.scaleBy(x: 1, y: -1)
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
            ctx.restoreGState()
        }
        if !recordingMode { (status as NSString).draw(in: NSRect(x: 6, y: 4, width: bounds.width-12, height: 36),
                                 withAttributes: [.foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: 10)])
        }
    }
}

final class Delegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var window: NSWindow!
    var recordingActivity: NSObjectProtocol?
    func applicationDidFinishLaunching(_ note: Notification) {
        guard CommandLine.arguments.count == 2 || CommandLine.arguments.count == 4 else { fputs("Clashback LIVE_DIR [WIDTH HEIGHT]\n", stderr); NSApp.terminate(nil); return }
        if let icon = Bundle.main.image(forResource: "Clashback") {
            NSApp.applicationIconImage = icon
        }
        let view = ReplayView(folder: URL(fileURLWithPath: CommandLine.arguments[1]))
        if view.recordingMode {
            recordingActivity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .latencyCritical],
                reason: "Continuous OBS replay recording")
        }
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1400, height: 900)
        let renderWidth = CommandLine.arguments.count == 4 ? Double(CommandLine.arguments[2]) ?? 720 : 720
        let renderHeight = CommandLine.arguments.count == 4 ? Double(CommandLine.arguments[3]) ?? 1600 : 1600
        let scale = min(min(1000.0, screen.height-186) / renderHeight, (screen.width-80) / renderWidth)
        let w = renderWidth*scale
        let h = view.recordingMode ? renderHeight*scale : renderHeight*scale+116
        let style: NSWindow.StyleMask = view.recordingMode ? [.borderless] : [.titled, .closable, .resizable, .miniaturizable]
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: w, height: h), styleMask: style,
                          backing: .buffered, defer: false)
        if view.recordingMode {
            // Keep the capture surface visible; AppKit may defer an occluded
            // window's backing-store redraw even while its frame reader runs.
            window.level = .floating
        }
        window.title = "Clashback"
        window.contentView = view; window.delegate = self
        window.center(); window.makeKeyAndOrderFront(nil); window.makeFirstResponder(view)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

func launchReplay() -> Int32 {
    let root = Bundle.main.bundleURL.resolvingSymlinksInPath().deletingLastPathComponent()
    let studio = root.appendingPathComponent("studio.py")
    let log = root.appendingPathComponent("cache/launcher.log")
    do {
        guard FileManager.default.fileExists(atPath: studio.path) else {
            throw NSError(domain: "Clashback", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "请把 Clashback.app 放在项目文件夹内，与 studio.py 放在同一层。"])
        }
        let path = ProcessInfo.processInfo.environment["PATH"] ?? ""
        let directories = path.split(separator: ":").map(String.init) +
            ["/usr/local/bin", "/Library/Frameworks/Python.framework/Versions/Current/bin",
             "/opt/homebrew/bin", "/usr/bin"]
        let python = directories.map { $0 + "/python3" }.first { candidate in
            guard FileManager.default.isExecutableFile(atPath: candidate) else { return false }
            let check = Process()
            check.executableURL = URL(fileURLWithPath: "/usr/bin/arch")
            check.arguments = ["-x86_64", candidate, "-c", "import sys; sys.exit(sys.version_info < (3, 11))"]
            check.standardOutput = FileHandle.nullDevice
            check.standardError = FileHandle.nullDevice
            do { try check.run() } catch { return false }
            check.waitUntilExit()
            return check.terminationStatus == 0
        }
        guard let python = python else {
            throw NSError(domain: "Clashback", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "请安装 Rosetta 2 和支持 x86_64 的 Python 3.11 或更高版本，例如 Python 3.12 Universal2。"])
        }
        try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: log)
        let output = try FileHandle(forWritingTo: log)
        defer { try? output.close() }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-Bu", studio.path, "open"]
        process.currentDirectoryURL = root
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = output
        try process.run()
        process.waitUntilExit()
        if process.terminationStatus == 0 { return 0 }
        let lines = (try? String(contentsOf: log, encoding: .utf8))?.split(separator: "\n") ?? []
        let detail = lines.last(where: { $0.hasPrefix("ERROR: ") }).map { String($0.dropFirst(7)) }
        throw NSError(domain: "Clashback", code: Int(process.terminationStatus), userInfo: [
            NSLocalizedDescriptionKey: [detail, "日志：\(log.path)"].compactMap { $0 }.joined(separator: "\n\n")])
    } catch {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Clashback 无法继续"
        alert.informativeText = error.localizedDescription
        alert.runModal()
        return 1
    }
}

if CommandLine.arguments.dropFirst().allSatisfy({ $0.hasPrefix("-psn_") }) {
    exit(launchReplay())
}

let app = NSApplication.shared
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--check-controls" {
    let folder = URL(fileURLWithPath: CommandLine.arguments[2])
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let view = ReplayView(folder: folder)
    func press(_ code: UInt16) {
        let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
            windowNumber: 0, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: code)!
        view.keyDown(with: event)
    }
    func command() -> String { (try? String(contentsOf: folder.appendingPathComponent("control.txt"), encoding: .utf8)) ?? "" }
    precondition(view.buttons.map { $0.title } == ["打开 HTML…", "播放/暂停", "重播"])
    view.tick = 200; press(123); precondition(command().contains("seek=180 ") && command().contains("paused=1"))
    view.tick = 200; press(124); precondition(command().contains("seek=220 "))
    view.tick = 2; press(123); precondition(command().contains("seek=2 "))
    view.tick = Int32(view.endTick); press(124); precondition(command().contains("seek=7200 "))
    view.tick = -1
    let before = command(); press(123); precondition(command() == before)
    view.setSpeed(0.5)
    for expected in [1.0, 2.0, 4.0, 8.0, 8.0] { press(126); precondition(view.speed == expected && command().contains("speed=\(expected) ")) }
    for expected in [4.0, 2.0, 1.0, 0.5, 0.5] { press(125); precondition(view.speed == expected && command().contains("speed=\(expected) ")) }
    print("{\"ok\":true,\"seek_seconds\":1,\"speed_steps\":[0.5,1,2,4,8],\"buttons\":3,\"boundary_checks\":true}")
    exit(0)
}
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "--check-image" {
    if let image = NSImage(contentsOfFile: CommandLine.arguments[2]),
       let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        print("{\"width\":\(cg.width),\"height\":\(cg.height),\"ok\":true}"); exit(0)
    }
    fputs("无法读取 PNG/JPEG 等背景图片，请检查文件格式和路径。\n", stderr); exit(2)
}
if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--choose-html" {
    app.setActivationPolicy(.regular); app.activate(ignoringOtherApps: true)
    let panel = htmlFilePicker()
    if panel.runModal() == .OK, let url = panel.url { print(url.path); exit(0) }
    exit(1)
}
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
