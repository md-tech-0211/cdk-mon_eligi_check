"""
Lambda: patient text → Bedrock Claude → structured JSON (per-policy comparison).

POST body (Function URL): JSON {"input": "..."} or any JSON with input/text/payload.
Response: JSON { "structured": {...}, "skipSecondaryBedrock": true } — no duplicate text field.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
from typing import Any

import boto3

_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_DIR, "trials_reference.txt"), encoding="utf-8") as _f:
    TRIALS_REFERENCE = _f.read()

bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
)

DEFAULT_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    #"arn:aws:bedrock:us-east-1:972841066642:inference-profile/us.anthropic.claude-sonnet-4-6",
)

JSON_OUTPUT_SPEC = """
You MUST respond with a single JSON object only (no markdown fences, no commentary before or after).
Use this exact shape and keys:

{
  "schema_version": "1.0",
  "patient_profile_summary": "<1 short paragraph: age, BMI if known, dx, meds, substances, episode, red flags>",
  "policies": [
    {
      "policy_name": "<exact study name from the reference table>",
      "domain": "MDD | BIPOLAR | SAD | PTSD",
      "validity": "eligible | ineligible | conditionally_eligible | insufficient_data",
      "screening_decision": "<one sentence: your final screening stance for this policy>",
      "reasons_for_validity": ["<specific criterion-based reasons this validity applies>"],
      "reasons_against_validity": ["<specific criterion-based blockers; empty if eligible>"],
      "minimum_gap_to_eligibility": "<null, or the smallest concrete change(s) to become eligible; if lifetime hard exclusion, say so>",
      "hard_lifetime_exclusion": <true|false>,
      "needs_crc_clarification": <true|false>,
      "crc_notes": ["<optional strings>"]
    }
  ],
  "final_recommendation": {
    "refer_immediately": ["<policy_name>", "..."],
    "revisit_when": [{"policy_name": "<name>", "when": "<condition>"}],
    "no_eligibility_path": ["<policy_name where lifetime or non-modifiable exclusion applies>"]
  }
}

Rules:
- Include EVERY named study from the reference (all policies), one object per policy, even if insufficient_data.
- `validity` must align with `reasons_for_validity` / `reasons_against_validity` (no contradictions).
- Apply the same clinical rules as in the narrative instructions (UDS, formal dx, episode windows, BMI bounds, etc.).
- Output valid JSON: double quotes, no trailing commas, no comments.
"""


def build_system_prompt(patient_data: str) -> str:
    return f"""You are a clinical research coordinator doing psychiatric trial pre-screening.

{TRIALS_REFERENCE}

Evaluate the patient against each policy (study) in the reference. Be precise and criterion-based.

Clinical rules (prescreen mode):
- Positive UDS may disqualify only where protocol relevant.
- Self-reported diagnosis consistent with symptoms may count as probable diagnosis for prescreening.
- Formal diagnosis can be confirmed later by site.
- Active episode means current symptoms likely present now.
- BMI hard cutoffs remain strict.
- Missing rating scales (MADRS/HDRS/HAMD/LSAS) should NOT cause automatic ineligible.
- Site can confirm scales later.

Eligibility optimization rules:
- Use prescreening logic, not final site adjudication.
- If core criteria fit and no clear exclusion exists, prefer eligible.
- Use conditionally_eligible when diagnosis, scales, or minor history need confirmation.
- Only explicit hard blockers should produce ineligible.
- Never invent symptom severity not stated.
- Do not reject only because formal scales are missing.


{JSON_OUTPUT_SPEC}

---

PATIENT DATA:
{patient_data}
"""


def parse_tsv_candidate(raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    if any("\t" in ln for ln in lines[:50]):
        buf = io.StringIO(text)
        try:
            reader = csv.reader(buf, delimiter="\t")
            rows = list(reader)
        except Exception:
            return f"CANDIDATE PROFILE (raw):\n\n{text}"
        if not rows:
            return f"CANDIDATE PROFILE (raw):\n\n{text}"
        header = [c.strip() for c in rows[0]]
        out_lines = ["CANDIDATE PROFILE (parsed TSV):", ""]
        if len(header) > 1 and all(header):
            for row in rows[1:]:
                if not any((c or "").strip() for c in row):
                    continue
                parts = []
                for i, col in enumerate(header):
                    val = row[i].strip() if i < len(row) else ""
                    if val:
                        parts.append(f"{col}: {val}")
                if parts:
                    out_lines.append(" | ".join(parts))
            out_lines.append("")
            return "\n".join(out_lines).strip()
    return f"CANDIDATE PROFILE (free text):\n\n{text}"


def _decode_http_body(event: dict[str, Any]) -> str:
    body = event.get("body")
    if body is None:
        return ""
    if not isinstance(body, str):
        return str(body)
    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8", errors="replace")
    return body


def parse_json_body(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def extract_from_payload_dict(d: dict[str, Any]) -> str:
    for key in ("input", "text", "payload", "patient_data", "patientData", "body"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return json.dumps(d, indent=2, ensure_ascii=False)


def extract_raw_text_from_event(event: Any) -> str:
    if isinstance(event, str):
        return event.strip()
    if not isinstance(event, dict):
        return str(event).strip()
    if "body" in event and event["body"] is not None:
        raw = _decode_http_body(event)
        if not raw.strip():
            return ""
        parsed = parse_json_body(raw)
        if isinstance(parsed, dict):
            return extract_from_payload_dict(parsed)
        return raw.strip()
    return extract_from_payload_dict(event)


def extract_json_object_from_llm_text(raw: str) -> dict[str, Any]:
    """Strip optional ```json fences and parse the first JSON object."""
    t = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
    if fence:
        t = fence.group(1).strip()
    # First { ... } balance heuristic
    start = t.find("{")
    if start < 0:
        raise ValueError("No JSON object found in model output")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise ValueError("Unbalanced braces in model output")


def lambda_handler(event, context):
    try:
        raw_text = extract_raw_text_from_event(event)
        if not raw_text:
            return _response_json(400, {"error": "No candidate information was provided."})

        candidate_profile = parse_tsv_candidate(raw_text)
        system_prompt = build_system_prompt(candidate_profile)

        user_prompt = (
            "Return ONLY the JSON object specified in the system message. "
            "Evaluate this candidate against every policy in the reference.\n\n"
            f"{candidate_profile}"
        )

        response = bedrock.invoke_model(
            modelId=DEFAULT_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 8000,
                    "temperature": 0,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                }
            ),
        )

        response_body = json.loads(response["body"].read())
        output_text = response_body["content"][0]["text"]

        try:
            structured = extract_json_object_from_llm_text(output_text)
        except (json.JSONDecodeError, ValueError) as e:
            return _response_json(
                200,
                {
                    "structured": None,
                    "parse_error": str(e),
                    "unparsed_model_output": output_text,
                    "skipSecondaryBedrock": True,
                },
            )

        return _response_json(
            200,
            {
                "structured": structured,
                "skipSecondaryBedrock": True,
            },
        )

    except Exception as e:
        return _response_json(500, {"error": f"Internal server error: {str(e)}"})


def _response_json(status_code: int, payload: dict[str, Any]) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(payload),
    }
