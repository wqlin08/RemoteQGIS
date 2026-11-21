// SPDX-License-Identifier: GPL-3.0-or-later

import CoreGraphics
import Foundation
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

// Run from the repository root to regenerate the Host Manager PNGs from logo.png.
// Export the Windows PNG to a multi-size ICO (16, 24, 32, 48, 64, 128, 256).
// Then use rsrc v0.10.2 with -arch amd64 to update packaging/windows/logo_windows_amd64.syso.
let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let source = CGImageSourceCreateWithURL(root.appendingPathComponent("logo.png") as CFURL, nil)!
let logo = CGImageSourceCreateImageAtIndex(source, 0, nil)!
let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!

func saveIcon(_ file: String, pixels: Int, inset: CGFloat, radius: CGFloat, continuous: Bool) {
    let canvas = CGRect(x: 0, y: 0, width: pixels, height: pixels)
    let scale = CGFloat(pixels) / 1024
    let frame = canvas.insetBy(dx: inset * scale, dy: inset * scale)
    let path = RoundedRectangle(
        cornerRadius: radius * scale, style: continuous ? .continuous : .circular
    ).path(in: frame).cgPath
    let context = CGContext(
        data: nil, width: pixels, height: pixels, bitsPerComponent: 8, bytesPerRow: 0,
        space: colorSpace, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    )!
    context.clear(canvas)
    context.addPath(path)
    context.clip()
    context.interpolationQuality = .high
    context.draw(logo, in: frame)
    let output = root.appendingPathComponent(file)
    let destination = CGImageDestinationCreateWithURL(output as CFURL, UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(destination, context.makeImage()!, nil)
    precondition(CGImageDestinationFinalize(destination), "Unable to write \(output.path)")
}

// Apple-style continuous shape on a padded 1024-point canvas.
saveIcon("packaging/macos/logo.png", pixels: 1024, inset: 100, radius: 185, continuous: true)
// Fluent exterior corner radius is 2 points on a 48-point grid.
saveIcon("packaging/windows/logo.png", pixels: 256, inset: 3 * 1024 / 48, radius: 2 * 1024 / 48, continuous: false)
