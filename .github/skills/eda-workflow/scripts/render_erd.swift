import AppKit
import Foundation

struct TableNode {
    let schema: String
    let name: String
    let primaryKeys: [String]
    let foreignKeys: [String]
}

struct Relationship {
    let fromSchema: String
    let fromTable: String
    let toSchema: String
    let toTable: String
}

func key(_ schema: String, _ table: String) -> String {
    "\(schema).\(table)"
}

func text(_ value: String, x: CGFloat, y: CGFloat, font: NSFont, color: NSColor) {
    let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color]
    NSAttributedString(string: value, attributes: attributes).draw(at: NSPoint(x: x, y: y))
}

func main() throws {
    guard CommandLine.arguments.count == 3 else {
        throw NSError(domain: "render_erd", code: 1, userInfo: [NSLocalizedDescriptionKey: "Usage: render_erd.swift input.json output.png"])
    }
    let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
    let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])
    let data = try Data(contentsOf: inputURL)
    guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          let tableRows = root["tables"] as? [[String: Any]],
          let relationshipRows = root["relationships"] as? [[String: Any]] else {
        throw NSError(domain: "render_erd", code: 2, userInfo: [NSLocalizedDescriptionKey: "Invalid diagram input"])
    }
    let nodes = tableRows.map { row in
        TableNode(schema: row["schema"] as? String ?? "", name: row["name"] as? String ?? "",
                  primaryKeys: row["pk"] as? [String] ?? [], foreignKeys: row["fk"] as? [String] ?? [])
    }
    let relationships = relationshipRows.map { row in
        Relationship(fromSchema: row["from_schema"] as? String ?? "", fromTable: row["from_table"] as? String ?? "",
                     toSchema: row["to_schema"] as? String ?? "", toTable: row["to_table"] as? String ?? "")
    }
    let schemas = Array(Set(nodes.map(\.schema))).sorted()
    let grouped = Dictionary(grouping: nodes, by: \.schema)
    let columnWidth: CGFloat = 430
    let columnGap: CGFloat = 54
    let nodeHeight: CGFloat = 72
    let rowGap: CGFloat = 23
    let margin: CGFloat = 42
    let heading: CGFloat = 64
    let maxRows = max(1, schemas.map { grouped[$0, default: []].count }.max() ?? 1)
    let width = margin * 2 + CGFloat(schemas.count) * columnWidth + CGFloat(max(0, schemas.count - 1)) * columnGap
    let height = margin * 2 + heading + CGFloat(maxRows) * (nodeHeight + rowGap)
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(width), pixelsHigh: Int(height), bitsPerSample: 8,
                                  samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                  bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    NSColor(calibratedRed: 0.97, green: 0.98, blue: 0.985, alpha: 1).setFill()
    NSBezierPath(rect: NSRect(x: 0, y: 0, width: width, height: height)).fill()

    var positions: [String: NSRect] = [:]
    let schemaColors: [NSColor] = [
        NSColor(calibratedRed: 0.88, green: 0.94, blue: 0.98, alpha: 1),
        NSColor(calibratedRed: 0.91, green: 0.96, blue: 0.90, alpha: 1),
        NSColor(calibratedRed: 0.99, green: 0.94, blue: 0.84, alpha: 1),
        NSColor(calibratedRed: 0.95, green: 0.90, blue: 0.95, alpha: 1),
        NSColor(calibratedRed: 0.98, green: 0.89, blue: 0.88, alpha: 1),
        NSColor(calibratedRed: 0.89, green: 0.94, blue: 0.93, alpha: 1),
    ]
    let dark = NSColor(calibratedRed: 0.16, green: 0.24, blue: 0.30, alpha: 1)
    let lineColor = NSColor(calibratedRed: 0.34, green: 0.43, blue: 0.49, alpha: 0.55)

    for (schemaIndex, schema) in schemas.enumerated() {
        let x = margin + CGFloat(schemaIndex) * (columnWidth + columnGap)
        text(schema, x: x, y: height - margin - 26, font: NSFont.boldSystemFont(ofSize: 21), color: dark)
        for (rowIndex, node) in grouped[schema, default: []].sorted(by: { $0.name < $1.name }).enumerated() {
            let y = height - margin - heading - CGFloat(rowIndex + 1) * nodeHeight - CGFloat(rowIndex) * rowGap
            positions[key(schema, node.name)] = NSRect(x: x, y: y, width: columnWidth, height: nodeHeight)
        }
    }

    for relation in relationships {
        guard let source = positions[key(relation.fromSchema, relation.fromTable)],
              let target = positions[key(relation.toSchema, relation.toTable)] else { continue }
        let sourceSchema = schemas.firstIndex(of: relation.fromSchema) ?? 0
        let targetSchema = schemas.firstIndex(of: relation.toSchema) ?? 0
        let start: NSPoint
        let end: NSPoint
        if sourceSchema != targetSchema {
            start = NSPoint(x: sourceSchema < targetSchema ? source.maxX : source.minX, y: source.midY)
            end = NSPoint(x: sourceSchema < targetSchema ? target.minX : target.maxX, y: target.midY)
        } else {
            start = NSPoint(x: source.midX, y: source.midY >= target.midY ? source.minY : source.maxY)
            end = NSPoint(x: target.midX, y: source.midY >= target.midY ? target.maxY : target.minY)
        }
        let path = NSBezierPath()
        path.lineWidth = 1.15
        path.move(to: start)
        if sourceSchema == targetSchema {
            path.curve(to: end, controlPoint1: NSPoint(x: start.x + 32, y: (start.y + end.y) / 2),
                       controlPoint2: NSPoint(x: end.x + 32, y: (start.y + end.y) / 2))
        } else {
            path.curve(to: end, controlPoint1: NSPoint(x: (start.x + end.x) / 2, y: start.y),
                       controlPoint2: NSPoint(x: (start.x + end.x) / 2, y: end.y))
        }
        lineColor.setStroke()
        path.stroke()
        let dx = end.x - start.x
        let dy = end.y - start.y
        let length = max(1, hypot(dx, dy))
        let ux = dx / length
        let uy = dy / length
        let base = NSPoint(x: end.x - ux * 9, y: end.y - uy * 9)
        let wing1 = NSPoint(x: base.x - uy * 4, y: base.y + ux * 4)
        let wing2 = NSPoint(x: base.x + uy * 4, y: base.y - ux * 4)
        let arrow = NSBezierPath()
        arrow.move(to: end)
        arrow.line(to: wing1)
        arrow.line(to: wing2)
        arrow.close()
        lineColor.setFill()
        arrow.fill()
    }

    for node in nodes {
        guard let rect = positions[key(node.schema, node.name)] else { continue }
        let schemaIndex = schemas.firstIndex(of: node.schema) ?? 0
        let box = NSBezierPath(roundedRect: rect, xRadius: 5, yRadius: 5)
        schemaColors[schemaIndex % schemaColors.count].setFill()
        box.fill()
        dark.setStroke()
        box.lineWidth = 1
        box.stroke()
        text(node.name, x: rect.minX + 9, y: rect.minY + 47, font: NSFont.boldSystemFont(ofSize: 13), color: dark)
        let keys = (node.primaryKeys.map { "PK \($0)" } + node.foreignKeys.map { "FK \($0)" }).joined(separator: "  |  ")
        let shown = keys.isEmpty ? "No key metadata" : String(keys.prefix(64))
        text(shown, x: rect.minX + 9, y: rect.minY + 18, font: NSFont.systemFont(ofSize: 9), color: dark)
    }

    NSGraphicsContext.restoreGraphicsState()
    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "render_erd", code: 3, userInfo: [NSLocalizedDescriptionKey: "PNG encoding failed"])
    }
    try png.write(to: outputURL)
    print("Rendered \(nodes.count) tables and \(relationships.count) foreign keys (\(Int(width))x\(Int(height))).")
}

do {
    try main()
} catch {
    fputs("ER diagram rendering failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}