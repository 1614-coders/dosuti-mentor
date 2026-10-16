import Foundation
import Vision
import ImageIO

struct Input: Decodable { let id: String; let path: String; let roi: [Double]? }
struct Box: Encodable { let x: Double; let y: Double; let width: Double; let height: Double }
struct Line: Encodable { let text: String; let confidence: Float; let box: Box }
struct Output: Encodable { let id: String; let seconds: Double; let lines: [Line]; let error: String? }

let inputs = try JSONDecoder().decode([Input].self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
var results: [Output] = []
for input in inputs {
    let start = Date()
    do {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.recognitionLanguages = ["en-US"]
        if let roi = input.roi {
            guard roi.count == 4, roi.allSatisfy({ $0.isFinite }), roi[0] >= 0, roi[1] >= 0,
                  roi[2] > 0, roi[3] > 0, roi[0]+roi[2] <= 1, roi[1]+roi[3] <= 1 else {
                throw NSError(domain: "OCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "Invalid ROI"])
            }
            // Input ROI: normalized top-left x,y,width,height. Vision uses bottom-left.
            request.regionOfInterest = CGRect(x: roi[0], y: 1-roi[1]-roi[3], width: roi[2], height: roi[3])
        }
        let handler = VNImageRequestHandler(url: URL(fileURLWithPath: input.path), options: [:])
        try handler.perform([request])
        let lines = (request.results ?? []).compactMap { observation -> Line? in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            let r = observation.boundingBox
            let roi = input.roi ?? [0, 0, 1, 1]
            // Vision observations are normalized within the requested ROI.
            let box = Box(x: roi[0] + r.minX * roi[2],
                          y: roi[1] + (1-r.maxY) * roi[3],
                          width: r.width * roi[2], height: r.height * roi[3])
            return Line(text: candidate.string, confidence: candidate.confidence,
                        box: box)
        }
        results.append(Output(id: input.id, seconds: Date().timeIntervalSince(start), lines: lines, error: nil))
    } catch {
        results.append(Output(id: input.id, seconds: Date().timeIntervalSince(start), lines: [], error: String(describing: error)))
    }
}
let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
FileHandle.standardOutput.write(try encoder.encode(results))
