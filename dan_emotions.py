from collections import Counter
from dataclasses import dataclass
from time import monotonic

import cv2
import numpy as np
from insightface.utils.face_align import norm_crop
from onnxruntime import InferenceSession, SessionOptions


MAX_BATCH = 8
VIEW_WINDOW_SECONDS = 1.0
RGB_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
RGB_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass(frozen=True)
class ExpressionModel:
    name: str
    filename: str
    size: int
    labels: tuple[str, ...]
    align: bool = False


EXPRESSION_MODELS = {
    "dan": ExpressionModel(
        "DAN (AffectNet-7)", "dan_affectnet7.onnx", 224,
        ("Neutral", "Happy", "Sad", "Surprise", "Fear", "Disgust", "Anger"), align=True,
    ),
    "hsemotion": ExpressionModel(
        "HSEmotion (enet_b2_7)", "hsemotion_enet_b2_7.onnx", 260,
        ("Anger", "Disgust", "Fear", "Happy", "Neutral", "Sad", "Surprise"),
    ),
}


class FaceExpressions:
    def __init__(self, model, model_path, device):
        self.model = model
        options = SessionOptions()
        options.intra_op_num_threads = 2
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        providers = ["CPUExecutionProvider"]
        if device == "cuda":
            providers.insert(0, "CUDAExecutionProvider")
        self.session = InferenceSession(str(model_path), sess_options=options, providers=providers)
        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        if (len(inputs) != 1 or len(outputs) != 1
                or inputs[0].type != "tensor(float)"
                or inputs[0].shape[1:] != [3, model.size, model.size]
                or isinstance(inputs[0].shape[0], int)
                or len(outputs[0].shape) != 2 or outputs[0].shape[1] != len(model.labels)):
            raise ValueError(f"Expected {model.name} ONNX with dynamic batch size, {model.size}x{model.size} input and seven logits")
        self.input_name = inputs[0].name
        self.output_name = outputs[0].name
        if device == "cuda":
            if "CUDAExecutionProvider" not in self.session.get_providers():
                raise RuntimeError(f"{model.name} could not initialize on CUDA")
            self.session.disable_fallback()
            self.probabilities([np.zeros((model.size, model.size, 3), dtype=np.uint8)])
        print(f"Expression providers: {', '.join(self.session.get_providers())}", flush=True)
        self.views = {}
        self.history = {}

    def probabilities(self, crops):
        pixels = np.stack(crops).astype(np.float32)
        rgb = (pixels[..., ::-1] / 255.0 - RGB_MEAN) / RGB_STD
        batch = np.ascontiguousarray(rgb.transpose(0, 3, 1, 2))
        logits, = self.session.run([self.output_name], {self.input_name: batch})
        exp = np.exp(logits - logits.max(axis=1, keepdims=True))
        return exp / exp.sum(axis=1, keepdims=True)

    def classify(self, frame, faces, names, sensor_id):
        labels = [None] * len(faces)
        crops = []
        indices = []
        for index, face in enumerate(faces):
            landmarks = face.kps
            if (landmarks is None or landmarks.shape != (5, 2) or not np.isfinite(landmarks).all()
                    or np.linalg.norm(landmarks[0] - landmarks[1]) < 1):
                continue
            if self.model.align:
                crop = norm_crop(frame, landmark=landmarks, image_size=self.model.size)
            else:
                left, top, right, bottom = map(int, face.bbox)
                left, top = max(0, left), max(0, top)
                right, bottom = min(frame.shape[1], right), min(frame.shape[0], bottom)
                if left >= right or top >= bottom:
                    continue
                crop = cv2.resize(frame[top:bottom, left:right], (self.model.size, self.model.size))
            crops.append(crop)
            indices.append(index)

        now = monotonic()
        self.views = {key: value for key, value in self.views.items() if now - value[0] <= VIEW_WINDOW_SECONDS}
        self.history = {key: value for key, value in self.history.items() if now - value[0] <= VIEW_WINDOW_SECONDS}
        occurrences = Counter(names)
        for name, count in occurrences.items():
            if count > 1:
                self.views.pop((name, sensor_id), None)

        for start in range(0, len(crops), MAX_BATCH):
            end = start + MAX_BATCH
            for index, scores in zip(indices[start:end], self.probabilities(crops[start:end]), strict=True):
                name = names[index]
                if name and name != "Unknown" and occurrences[name] == 1:
                    scores = self.merge_views(name, sensor_id, now, faces[index], scores)
                labels[index] = self.model.labels[int(scores.argmax())]
        return labels

    def merge_views(self, name, sensor_id, now, face, scores):
        landmarks = face.kps
        left_distance = np.linalg.norm(landmarks[2] - landmarks[0])
        right_distance = np.linalg.norm(landmarks[2] - landmarks[1])
        frontal = 1 - abs(left_distance - right_distance) / (left_distance + right_distance + 1e-6)
        size = min(1.0, (face.bbox[2] - face.bbox[0]) / 100.0)
        quality = max(0.001, float(face.det_score) * size * (0.3 + 0.7 * frontal ** 2))
        self.views[name, sensor_id] = (now, scores, quality)
        views = [(probability, weight) for (identity, _), (_, probability, weight) in self.views.items()
                 if identity == name]
        probabilities, weights = zip(*views)
        fused = np.average(probabilities, axis=0, weights=weights)
        if name in self.history:
            fused = 0.6 * fused + 0.4 * self.history[name][1]
        self.history[name] = (now, fused)
        return fused
