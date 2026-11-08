// iPhoneCam - streams the iPhone camera as length-prefixed JPEG frames over TCP.
// Reached from the computer through USB (usbmuxd port forwarding), so no Wi-Fi is needed.
//
// Frame format: [4-byte big-endian length][JPEG bytes], repeated.

import SwiftUI
import AVFoundation
import Network
import UIKit

final class Client {
    let conn: NWConnection
    var busy = false
    init(_ c: NWConnection) { conn = c }
}

final class CameraServer: NSObject, ObservableObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    @Published var status = "Starting..."
    @Published var clientCount = 0

    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "iphonecam.queue")
    private let ciContext = CIContext()
    private var listener: NWListener?
    private var clients: [ObjectIdentifier: Client] = [:]
    private let port: NWEndpoint.Port = 5005
    private var usingFront = false
    private var targetWidth: Int32 = 1280
    private var targetHeight: Int32 = 720
    private var targetFPS: Int = 30

    // Camera controls, set by text commands from the PC and re-applied whenever
    // the camera is reconfigured (resolution change, front/back switch).
    private var currentDevice: AVCaptureDevice?
    private var focusMode = "AUTO"            // AUTO | LOCK | MANUAL
    private var lensPosition: Float = 0.5     // 0 = near, 1 = far
    private var focusPoint: CGPoint?          // normalized, sensor orientation
    private var exposureMode = "AUTO"         // AUTO | MANUAL
    private var exposureBias: Float = 0
    private var manualISO: Float = 200
    private var shutterDenominator: Double = 60
    private var wbKelvin: Float = 0           // 0 = automatic
    private var zoomFactor: CGFloat = 1
    private var torchOn = false

    func start() {
        UIApplication.shared.isIdleTimerDisabled = true
        AVCaptureDevice.requestAccess(for: .video) { ok in
            DispatchQueue.main.async {
                if ok { self.configureCamera(); self.startListener() }
                else { self.status = "Camera permission denied (Settings > iPhoneCam)" }
            }
        }
    }

    func toggleCamera() {
        usingFront.toggle()
        queue.async { self.configureCamera() }
    }

    private func configureCamera() {
        session.beginConfiguration()
        session.sessionPreset = .hd1280x720
        session.inputs.forEach { session.removeInput($0) }
        session.outputs.forEach { session.removeOutput($0) }

        let position: AVCaptureDevice.Position = usingFront ? .front : .back
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: position),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else {
            session.commitConfiguration()
            DispatchQueue.main.async { self.status = "No camera available" }
            return
        }
        session.addInput(input)
        applyFormat(device)
        currentDevice = device
        applySettings()

        let output = AVCaptureVideoDataOutput()
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        if session.canAddOutput(output) { session.addOutput(output) }

        if let conn = output.connection(with: .video) {
            if #available(iOS 17.0, *) {
                if conn.isVideoRotationAngleSupported(0) { conn.videoRotationAngle = 0 }
            } else if conn.isVideoOrientationSupported {
                conn.videoOrientation = .landscapeRight
            }
            if conn.isVideoMirroringSupported { conn.isVideoMirrored = false }
        }
        session.commitConfiguration()
        if !session.isRunning { session.startRunning() }
    }

    // Pick a camera format matching the requested size and frame rate.
    // If the camera can't do it, the 720p preset stays in effect.
    private func applyFormat(_ device: AVCaptureDevice) {
        let fps = Double(targetFPS)
        let match = device.formats.first { f in
            let d = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
            return d.width == targetWidth && d.height == targetHeight
                && f.videoSupportedFrameRateRanges.contains { $0.maxFrameRate >= fps }
        }
        guard let format = match else { return }
        do {
            try device.lockForConfiguration()
            device.activeFormat = format
            let duration = CMTime(value: 1, timescale: CMTimeScale(targetFPS))
            device.activeVideoMinFrameDuration = duration
            device.activeVideoMaxFrameDuration = duration
            device.unlockForConfiguration()
        } catch {
            DispatchQueue.main.async { self.status = "Could not set format: \(error)" }
        }
    }

    // The computer sends a line like "CONFIG 1920 1080 60\n" right after connecting.
    private func receiveCommands(_ conn: NWConnection, buffer: String = "") {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 256) { [weak self] data, _, isComplete, error in
            guard let self = self else { return }
            var buf = buffer
            if let data = data, let s = String(data: data, encoding: .utf8) { buf += s }
            while let nl = buf.firstIndex(of: "\n") {
                let line = String(buf[..<nl]).trimmingCharacters(in: .whitespacesAndNewlines)
                buf = String(buf[buf.index(after: nl)...])
                self.handleCommand(line)
            }
            if error == nil && !isComplete { self.receiveCommands(conn, buffer: buf) }
        }
    }

    // Commands (one per line) from the PC:
    //   CONFIG <w> <h> <fps>              FOCUS AUTO | LOCK | MANUAL <0..1>
    //   FOCUSPOINT <x> <y>                EXPOSURE AUTO <bias> | MANUAL <iso> <shutter denominator>
    //   WB AUTO | KELVIN <k>              ZOOM <factor>
    //   TORCH <0|1>                       CAMERA FRONT | BACK
    private func handleCommand(_ line: String) {
        let p = line.split(separator: " ").map(String.init)
        guard let cmd = p.first else { return }
        func num(_ i: Int) -> Double? { i < p.count ? Double(p[i]) : nil }

        switch cmd {
        case "CONFIG":
            guard let w = num(1), let h = num(2), let f = num(3),
                  w >= 1, w < 10000, h >= 1, h < 10000, f >= 1, f <= 240 else { return }
            let nw = Int32(w), nh = Int32(h), nf = Int(f)
            if nw == targetWidth && nh == targetHeight && nf == targetFPS { return }
            targetWidth = nw; targetHeight = nh; targetFPS = nf
            configureCamera()
        case "FOCUS":
            guard p.count >= 2 else { return }
            focusMode = p[1]
            if let v = num(2) { lensPosition = Float(min(max(v, 0), 1)) }
            focusPoint = nil
            applySettings()
        case "FOCUSPOINT":
            guard let x = num(1), let y = num(2) else { return }
            focusMode = "AUTO"
            focusPoint = CGPoint(x: min(max(x, 0), 1), y: min(max(y, 0), 1))
            applySettings()
        case "EXPOSURE":
            guard p.count >= 2 else { return }
            exposureMode = p[1]
            if exposureMode == "MANUAL" {
                if let iso = num(2) { manualISO = Float(iso) }
                if let den = num(3), den >= 1, den <= 100000 { shutterDenominator = den }
            } else if let bias = num(2) {
                exposureBias = Float(bias)
            }
            applySettings()
        case "WB":
            guard p.count >= 2 else { return }
            wbKelvin = (p[1] == "KELVIN") ? Float(num(2) ?? 0) : 0
            applySettings()
        case "ZOOM":
            guard let z = num(1) else { return }
            zoomFactor = CGFloat(z)
            applySettings()
        case "TORCH":
            torchOn = (num(1) ?? 0) != 0
            applySettings()
        case "CAMERA":
            guard p.count >= 2 else { return }
            let front = (p[1] == "FRONT")
            if front != usingFront { usingFront = front; configureCamera() }
        default:
            break
        }
    }

    // Push the stored control values to the camera, clamped to what it supports.
    private func applySettings() {
        guard let device = currentDevice else { return }
        do { try device.lockForConfiguration() } catch { return }
        defer { device.unlockForConfiguration() }

        // Focus
        switch focusMode {
        case "MANUAL":
            if device.isLockingFocusWithCustomLensPositionSupported {
                device.setFocusModeLocked(lensPosition: lensPosition, completionHandler: nil)
            }
        case "LOCK":
            if device.isFocusModeSupported(.locked) { device.focusMode = .locked }
        default:
            if let p = focusPoint, device.isFocusPointOfInterestSupported {
                device.focusPointOfInterest = p
            }
            if device.isFocusModeSupported(.continuousAutoFocus) {
                device.focusMode = .continuousAutoFocus
            }
        }

        // Exposure
        if exposureMode == "MANUAL" {
            let f = device.activeFormat
            let iso = min(max(manualISO, f.minISO), f.maxISO)
            var d = CMTime(value: 1, timescale: CMTimeScale(shutterDenominator))
            if CMTimeCompare(d, f.minExposureDuration) < 0 { d = f.minExposureDuration }
            if CMTimeCompare(d, device.activeVideoMaxFrameDuration) > 0 { d = device.activeVideoMaxFrameDuration }
            device.setExposureModeCustom(duration: d, iso: iso, completionHandler: nil)
        } else {
            if focusMode == "AUTO", let p = focusPoint, device.isExposurePointOfInterestSupported {
                device.exposurePointOfInterest = p
            }
            if device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposureMode = .continuousAutoExposure
            }
            let b = min(max(exposureBias, device.minExposureTargetBias), device.maxExposureTargetBias)
            device.setExposureTargetBias(b, completionHandler: nil)
        }

        // White balance
        if wbKelvin > 0 {
            if device.isLockingWhiteBalanceWithCustomDeviceGainsSupported {
                let tt = AVCaptureDevice.WhiteBalanceTemperatureAndTintValues(temperature: wbKelvin, tint: 0)
                var g = device.deviceWhiteBalanceGains(for: tt)
                let m = device.maxWhiteBalanceGain
                g.redGain = min(max(g.redGain, 1), m)
                g.greenGain = min(max(g.greenGain, 1), m)
                g.blueGain = min(max(g.blueGain, 1), m)
                device.setWhiteBalanceModeLocked(with: g, completionHandler: nil)
            }
        } else if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
            device.whiteBalanceMode = .continuousAutoWhiteBalance
        }

        // Zoom (capped at 10x)
        let maxZoom = min(device.activeFormat.videoMaxZoomFactor, 10)
        device.videoZoomFactor = min(max(zoomFactor, device.minAvailableVideoZoomFactor), maxZoom)

        // Torch (back camera only)
        if device.hasTorch && device.isTorchAvailable {
            device.torchMode = torchOn ? .on : .off
        }
    }

    private func startListener() {
        do {
            let l = try NWListener(using: .tcp, on: port)
            l.newConnectionHandler = { [weak self] conn in self?.accept(conn) }
            l.stateUpdateHandler = { [weak self] state in
                DispatchQueue.main.async {
                    switch state {
                    case .ready: self?.status = "Ready on port 5005. Waiting for computer..."
                    case .failed(let e): self?.status = "Listener failed: \(e)"
                    default: break
                    }
                }
            }
            l.start(queue: queue)
            listener = l
        } catch {
            status = "Could not start server: \(error)"
        }
    }

    private func accept(_ conn: NWConnection) {
        let client = Client(conn)
        let id = ObjectIdentifier(client)
        clients[id] = client
        conn.stateUpdateHandler = { [weak self] state in
            guard let self = self else { return }
            switch state {
            case .failed, .cancelled:
                self.queue.async { self.clients[id] = nil; self.publishCount() }
            default: break
            }
        }
        conn.start(queue: queue)
        receiveCommands(conn)
        publishCount()
    }

    private func publishCount() {
        let n = clients.count
        DispatchQueue.main.async {
            self.clientCount = n
            self.status = n > 0 ? "Streaming to \(n) client(s)" : "Ready on port 5005. Waiting for computer..."
        }
    }

    // MARK: frame delivery
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard !clients.isEmpty,
              let pixel = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ci = CIImage(cvPixelBuffer: pixel)
        let space = CGColorSpace(name: CGColorSpace.sRGB)!
        guard let jpeg = ciContext.jpegRepresentation(
            of: ci, colorSpace: space,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.7]) else { return }

        var length = UInt32(jpeg.count).bigEndian
        var packet = Data(bytes: &length, count: 4)
        packet.append(jpeg)

        for client in clients.values where !client.busy {
            client.busy = true // drop frames for slow clients instead of queueing
            client.conn.send(content: packet, completion: .contentProcessed { [weak self] _ in
                self?.queue.async { client.busy = false }
            })
        }
    }
}

@main
struct iPhoneCamApp: App {
    @StateObject private var server = CameraServer()

    var body: some Scene {
        WindowGroup {
            VStack(spacing: 20) {
                Image(systemName: "video.fill").font(.system(size: 48))
                Text("iPhoneCam").font(.title.bold())
                Text(server.status).multilineTextAlignment(.center)
                Text("Computer connected: \(server.clientCount)").foregroundStyle(.secondary)
                Button("Switch front/back camera") { server.toggleCamera() }
                    .buttonStyle(.borderedProminent)
                Text("Keep this app open and the phone unlocked.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            .padding()
            .onAppear { server.start() }
        }
    }
}
