// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "AriaKit",
    platforms: [
        .iOS(.v17),
        .macOS(.v13),
    ],
    products: [
        .library(name: "AriaKit", targets: ["AriaKit"]),
    ],
    targets: [
        .target(
            name: "AriaKit",
            path: "Sources/AriaKit"
        ),
        .testTarget(
            name: "AriaKitTests",
            dependencies: ["AriaKit"],
            path: "Tests/AriaKitTests"
        ),
    ]
)
