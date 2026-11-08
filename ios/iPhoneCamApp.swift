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
