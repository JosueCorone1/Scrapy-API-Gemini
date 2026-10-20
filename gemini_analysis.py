"""Small REST client for an optional Gemini text analysis."""
import json
import re

MAX_ANALYSIS_CHARS = 100_000

SYSTEM_PROMPT = """Actúa como un analista de lenguaje con conocimientos del modelo Big Five.
Analiza únicamente patrones observables en los textos entregados. Esto es una
impresión lingüística especulativa, no una prueba psicométrica validada, un
diagnóstico ni una descripción segura de la personalidad real de la persona.
No infieras salud mental, identidad, ideología u otros datos sensibles. Si hay
poca evidencia para un rasgo, usa score null y explica la limitación.

Devuelve solo JSON válido con este esquema:
{
  "apertura_a_la_experiencia": {"score": 1-100 o null, "justificacion": "breve"},
  "responsabilidad": {"score": 1-100 o null, "justificacion": "breve"},
  "extraversion": {"score": 1-100 o null, "justificacion": "breve"},
  "amabilidad": {"score": 1-100 o null, "justificacion": "breve"},
  "neuroticismo": {"score": 1-100 o null, "justificacion": "breve"},
  "limitaciones": "breve explicación de los límites de esta lectura"
}
Cada justificación debe ocupar como máximo dos frases y basarse solo en el
tono, las palabras y el estilo de los textos. No presentes la escala como una
medición clínica ni como una conclusión sobre la persona."""

_MENTION_RE = re.compile(r"(?<![\w.])@[A-Za-z0-9._]+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ().-]{7,}\d)(?!\w)")


def _prepare_text(profile, content):
    chunks = []
    biography = profile.get("biography") or ""
    if biography.strip():
        chunks.append(f"Biografía:\n{biography}")
    for index, item in enumerate(content, 1):
        caption = item.get("caption") or ""
        if caption.strip():
            chunks.append(f"Texto {index}:\n{caption}")

    text = "\n\n".join(chunks)
    text = _EMAIL_RE.sub("[correo oculto]", text)
    text = _MENTION_RE.sub("@mención", text)
    text = _PHONE_RE.sub("[teléfono oculto]", text)
    was_truncated = len(text) > MAX_ANALYSIS_CHARS
    if was_truncated:
        text = text[:MAX_ANALYSIS_CHARS] + "\n[Texto truncado por límite de tamaño]"
    return text, was_truncated


def analyze_texts(profile, content, *, api_key, model):
    """POST the selected bio/captions to Gemini and parse its JSON response."""
    text, was_truncated = _prepare_text(profile, content)
    if not text.strip():
        raise ValueError("No hay texto para analizar.")
    if not api_key:
        raise ValueError("Falta GEMINI_API_KEY.")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "Falta google-genai. Instala las dependencias con "
            "'python -m pip install -r requirements.txt'."
        ) from exc

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
            ),
        )
    except Exception as exc:
        message = str(exc)
        if isinstance(exc, TimeoutError) or "timeout" in message.casefold():
            raise TimeoutError(
                "La conexión con Gemini agotó el tiempo de espera. "
                "Comprueba internet, proxy o firewall y vuelve a intentarlo."
            ) from None
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        detail = f" HTTP {status}" if status else ""
        raise RuntimeError(
            f"Gemini no pudo completar la petición{detail}: {message[:350]}"
        ) from None
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    response_text = getattr(response, "text", None)
    if not response_text:
        raise RuntimeError("Gemini respondió sin texto JSON.")
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini no devolvió el JSON de análisis esperado.") from exc

    required_traits = (
        "apertura_a_la_experiencia",
        "responsabilidad",
        "extraversion",
        "amabilidad",
        "neuroticismo",
    )
    if not isinstance(result, dict):
        raise RuntimeError("Gemini no devolvió un objeto JSON.")
    for trait in required_traits:
        entry = result.get(trait)
        if not isinstance(entry, dict):
            raise RuntimeError(f"Falta el resultado JSON para {trait}.")
        score = entry.get("score")
        if score is not None and (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 1 <= score <= 100
        ):
            raise RuntimeError(f"La puntuación JSON de {trait} no está entre 1 y 100.")
        if not isinstance(entry.get("justificacion"), str):
            raise RuntimeError(f"Falta la justificación JSON para {trait}.")
    result["_input_truncated"] = was_truncated
    return result
