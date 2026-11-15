#!/usr/bin/env python3
"""ワークフロー JSON（workflow/ai-mail-triage.json）を生成する。

ノードの JavaScript と配置をこのファイルで管理し、JSON を手で編集しない。
  python3 tools/build_workflow.py
"""
import json
import pathlib
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "workflow" / "ai-mail-triage.json"

# Webhook の識別子は固定値にする（生成のたびに差分が出ないように）
WEBHOOK_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "n8n-ai-mail-triage/receive-inquiry"))
WEBHOOK_PATH = "inquiry-triage"

# ---------------------------------------------------------------- コード

VALIDATE_INPUT_JS = r"""// 入力検証: 件名と本文の有無・型・長さを確かめる。LLM はまだ呼ばない。
const config = $input.first().json;
const raw = $('Receive Inquiry').first().json.body;
const errors = [];
const isObject = raw !== null && typeof raw === 'object' && !Array.isArray(raw);
const src = isObject ? raw : {};
const text = (v) => (typeof v === 'string' ? v.trim() : '');

if (!isObject) errors.push('リクエスト本文が JSON オブジェクトではありません');
const email = { from: text(src.from), subject: text(src.subject), body: text(src.body) };
if (isObject) {
  if (!email.subject) errors.push('subject（件名）がありません');
  if (!email.body) errors.push('body（本文）がありません');
  if (email.body.length > config.max_body_chars) {
    errors.push(`body（本文）が長すぎます（${config.max_body_chars}文字まで）`);
  }
}

return [{
  json: {
    inputValid: errors.length === 0,
    inputErrors: errors,
    email,
    config,
    attempt: 1,
    lastValidationError: null,
    receivedAt: new Date().toISOString(),
  },
}];
"""

BUILD_PROMPT_JS = r"""// LLM への依頼文を組み立てる。差出人のアドレスは送らない（件名と本文のみ）。
const ctx = $input.first().json;

const system = [
  'あなたは問い合わせメールの仕分け担当です。',
  'メールを読み、次のキーだけを持つ JSON オブジェクトを1つ返してください。説明文は不要です。',
  '- category: "見積依頼" / "不具合報告" / "請求関連" / "その他" のいずれか',
  '- company: 差出人の会社名。書かれていなければ null',
  '- contact_name: 差出人の担当者名（氏名）。書かれていなければ null',
  '- summary: 要件の要約（日本語・200文字以内）',
  '- urgency: "高" / "中" / "低" のいずれか。業務停止や期限の明示があれば "高"',
].join('\n');

const messages = [
  { role: 'system', content: system },
  { role: 'user', content: `件名: ${ctx.email.subject}\n\n本文:\n${ctx.email.body}` },
];
if (ctx.lastValidationError) {
  // 再依頼: 前回の不正理由を伝えて、形式を直させる
  messages.push({
    role: 'user',
    content: `前回の回答は次の理由で不正でした: ${ctx.lastValidationError}\n指定のキーだけを持つ JSON オブジェクトを返してください。`,
  });
}

const requestBody = {
  model: ctx.config.llm_model,
  messages,
  temperature: 0,
  max_tokens: ctx.config.llm_max_tokens,
  response_format: { type: 'json_object' },
};

return [{ json: { ...ctx, requestBody } }];
"""

VALIDATE_OUTPUT_JS = r"""// LLM の回答を JSON として読み、スキーマ（必須・型・選択肢・文字数）で検証する。
const ctx = $('Build Prompt').last().json;
const res = $input.first().json;
const CATEGORIES = ['見積依頼', '不具合報告', '請求関連', 'その他'];
const URGENCIES = ['高', '中', '低'];
const REQUIRED = ['category', 'company', 'contact_name', 'summary', 'urgency'];

function validate(content) {
  if (typeof content !== 'string' || !content.trim()) return { error: '回答が空です' };
  // コードブロックで囲まれて返る提供元があるため、囲みだけは外す
  const s = content.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
  let obj;
  try { obj = JSON.parse(s); } catch (e) { return { error: 'JSON として読めません' }; }
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
    return { error: 'JSON オブジェクトではありません' };
  }
  const errs = [];
  for (const k of REQUIRED) if (!(k in obj)) errs.push(`${k} がありません`);
  if ('category' in obj && !CATEGORIES.includes(obj.category)) errs.push('category が選択肢にありません');
  if ('urgency' in obj && !URGENCIES.includes(obj.urgency)) errs.push('urgency が選択肢にありません');
  for (const [k, max] of [['company', 100], ['contact_name', 50]]) {
    if (k in obj && obj[k] !== null && (typeof obj[k] !== 'string' || obj[k].length > max)) {
      errs.push(`${k} は${max}文字以内の文字列か null にしてください`);
    }
  }
  if ('summary' in obj && (typeof obj.summary !== 'string' || !obj.summary.trim() || obj.summary.length > 200)) {
    errs.push('summary は1〜200文字の文字列にしてください');
  }
  if (errs.length) return { error: errs.join(' / ') };
  const orNull = (v) => (typeof v === 'string' && v.trim() ? v.trim() : null);
  // スキーマにないキーはここで落とす
  return {
    value: {
      category: obj.category,
      company: orNull(obj.company),
      contact_name: orNull(obj.contact_name),
      summary: obj.summary.trim(),
      urgency: obj.urgency,
    },
  };
}

const content = res && res.choices && res.choices[0] && res.choices[0].message
  ? res.choices[0].message.content : undefined;
const result = validate(content);
const { requestBody, ...rest } = ctx;

return [{
  json: {
    ...rest,
    outputValid: !result.error,
    validationError: result.error || null,
    extracted: result.value || null,
  },
}];
"""

PREPARE_RETRY_JS = r"""// 再依頼の準備: 試行回数を進め、不正理由を次の依頼文に渡す。
const ctx = $input.first().json;
return [{
  json: {
    ...ctx,
    attempt: ctx.attempt + 1,
    lastValidationError: ctx.validationError,
    outputValid: undefined,
    validationError: undefined,
  },
}];
"""

BUILD_RECORD_JS = r"""// 記録用の行データ（表計算シート・データテーブルの1行に相当）と応答を作る。
const j = $input.first().json;
const x = j.extracted;
const record = {
  received_at: j.receivedAt,
  category: x.category,
  route: j.route,
  urgency: x.urgency,
  company: x.company || '',
  contact_name: x.contact_name || '',
  summary: x.summary,
  from: j.email.from,
  subject: j.email.subject,
  status: '未対応',
};
return [{
  json: {
    httpStatus: 200,
    response: {
      status: 'ok',
      category: x.category,
      extracted: x,
      route: j.route,
      record,
      replyDraft: j.replyDraft,
      attempts: j.attempt,
    },
  },
}];
"""

BUILD_INPUT_ERROR_JS = r"""// 入力エラー: LLM を呼ばずに 400 を返す。
const j = $input.first().json;
const error = { stage: 'input', reason: j.inputErrors.join(' / ') };
return [{
  json: {
    httpStatus: 400,
    error,
    response: { status: 'input_error', error },
    logContext: { receivedAt: j.receivedAt, attempts: 0 },
  },
}];
"""

CAPTURE_LLM_ERROR_JS = r"""// LLM 呼び出しの失敗（再試行のあと）を受け取り、共通のエラー経路へ渡す。
const ctx = $('Build Prompt').last().json;
const err = $input.first().json.error;
const reason = (err && (err.message || err.description)) || 'LLM の呼び出しに失敗しました';
const { requestBody, ...rest } = ctx;
return [{ json: { ...rest, failure: { stage: 'llm_call', reason: String(reason).slice(0, 300) } } }];
"""

BUILD_FALLBACK_JS = r"""// 要確認の記録: 分類できなかった問い合わせも取りこぼさず、人の確認に回す。
const j = $input.first().json;
const error = j.failure
  || { stage: 'llm_output', reason: j.validationError || '出力の検証に失敗しました' };
const record = {
  received_at: j.receivedAt,
  category: '要確認',
  route: '総合窓口',
  urgency: '',
  company: '',
  contact_name: '',
  summary: '',
  from: j.email.from,
  subject: j.email.subject,
  status: '要確認',
};
return [{
  json: {
    httpStatus: 200,
    error,
    response: { status: 'needs_review', category: '要確認', record, error, attempts: j.attempt },
    logContext: { receivedAt: j.receivedAt, attempts: j.attempt },
  },
}];
"""

LOG_ERROR_JS = r"""// エラーログ: 段階・理由・時刻・試行回数を1行にまとめる。本文は残さない。
// 保存先（表計算シート・チャット通知など）はこのノードの後ろに接続する。
const j = $input.first().json;
const logEntry = {
  logged_at: new Date().toISOString(),
  received_at: j.logContext.receivedAt,
  stage: j.error.stage,
  reason: j.error.reason,
  attempts: j.logContext.attempts,
  execution_id: $execution.id,
  workflow: $workflow.name,
};
return [{ json: { ...j, logEntry } }];
"""

# 分類ごとの担当窓口と返信下書きの本文（宛名と結びは共通）
ROUTES = [
    ("見積依頼", "Prepare Quote Reply", "営業",
     "お見積りのご依頼をいただき、誠にありがとうございます。\n"
     "正確なお見積りのため、対象の業務・利用人数・ご希望の時期をあらためて確認させていただきます。\n"
     "担当より2営業日以内にご連絡いたします。"),
    ("不具合報告", "Prepare Bug Reply", "サポート",
     "ご不便をおかけしており、申し訳ございません。\n"
     "調査のため、発生日時・操作の手順・表示された画面の内容をお知らせいただけますでしょうか。\n"
     "確認でき次第、担当よりご連絡いたします。"),
    ("請求関連", "Prepare Billing Reply", "経理",
     "請求に関するお問い合わせをいただき、ありがとうございます。\n"
     "内容を確認のうえ、担当よりあらためてご連絡いたします。"),
    ("その他", "Prepare General Reply", "総合窓口",
     "お問い合わせいただき、ありがとうございます。\n"
     "内容を確認のうえ、担当よりご連絡いたします。"),
]

REPLY_HEAD = ("={{ $json.extracted.company ? $json.extracted.company + '\\n' : '' }}"
              "{{ $json.extracted.contact_name ? $json.extracted.contact_name + ' 様' : 'ご担当者 様' }}\n\n")
REPLY_TAIL = "\n\n今後ともよろしくお願いいたします。\n\n【署名】"

# ---------------------------------------------------------------- ノード


def node(name, type_, version, pos, params, **extra):
    n = {"parameters": params, "name": name, "type": type_, "typeVersion": version,
         "position": list(pos), "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "n8n-ai-mail-triage/" + name))}
    n.update(extra)
    return n


def code(name, pos, js):
    return node(name, "n8n-nodes-base.code", 2, pos, {"jsCode": js})


def if_true(name, pos, left):
    """left（式）が true のとき 0番目の出力へ進む IF ノード。"""
    return node(name, "n8n-nodes-base.if", 2.2, pos, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "cond/" + name)),
                "leftValue": left, "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "options": {},
    })


def switch_by_category(name, pos):
    rules = []
    for cat, *_ in ROUTES:
        rules.append({
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "rule/" + cat)),
                    "leftValue": "={{ $json.extracted.category }}", "rightValue": cat,
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
            "renameOutput": True, "outputKey": cat,
        })
    return node(name, "n8n-nodes-base.switch", 3.2, pos, {"rules": {"values": rules}, "options": {}})


def assignment(name, value, type_="string"):
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "asg/" + name)), "name": name, "value": value, "type": type_}


def build():
    nodes = [
        node("Receive Inquiry", "n8n-nodes-base.webhook", 2, (0, 300), {
            "httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "responseNode", "options": {},
        }, webhookId=WEBHOOK_ID),
        node("Set Config", "n8n-nodes-base.set", 3.4, (220, 300), {
            "assignments": {"assignments": [
                assignment("llm_base_url", "http://host.docker.internal:11434/v1"),
                assignment("llm_model", "qwen2.5:7b"),
                assignment("llm_max_tokens", 400, "number"),
                assignment("max_schema_retries", 1, "number"),
                assignment("max_body_chars", 20000, "number"),
            ]},
            "options": {},
        }),
        code("Validate Input", (440, 300), VALIDATE_INPUT_JS),
        if_true("Input OK?", (660, 300), "={{ $json.inputValid }}"),
        code("Build Prompt", (880, 200), BUILD_PROMPT_JS),
        node("Call LLM", "n8n-nodes-base.httpRequest", 4.2, (1100, 200), {
            "method": "POST",
            "url": "={{ $json.config.llm_base_url }}/chat/completions",
            "sendBody": True, "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json.requestBody) }}",
            "options": {"timeout": 120000},
        }, retryOnFail=True, maxTries=3, waitBetweenTries=2000, onError="continueErrorOutput"),
        code("Validate LLM Output", (1320, 200), VALIDATE_OUTPUT_JS),
        if_true("Output Valid?", (1540, 200), "={{ $json.outputValid }}"),
        if_true("Can Retry?", (1760, 480), "={{ $json.attempt <= $json.config.max_schema_retries }}"),
        code("Prepare Retry", (1540, 640), PREPARE_RETRY_JS),
        switch_by_category("Route by Category", (1760, 100)),
        *[node(nm, "n8n-nodes-base.set", 3.4, (1980, -140 + i * 160), {
            "assignments": {"assignments": [
                assignment("route", route),
                assignment("replyDraft", REPLY_HEAD + text + REPLY_TAIL),
            ]},
            "includeOtherFields": True, "options": {},
        }) for i, (_, nm, route, text) in enumerate(ROUTES)],
        code("Build Record", (2200, 100), BUILD_RECORD_JS),
        node("Respond Success", "n8n-nodes-base.respondToWebhook", 1.1, (2420, 100), {
            "respondWith": "json", "responseBody": "={{ $json.response }}",
            "options": {"responseCode": "={{ $json.httpStatus }}"},
        }),
        code("Build Input Error", (880, 560), BUILD_INPUT_ERROR_JS),
        code("Capture LLM Error", (1320, 400), CAPTURE_LLM_ERROR_JS),
        code("Build Fallback Record", (1980, 640), BUILD_FALLBACK_JS),
        code("Log Error", (2200, 640), LOG_ERROR_JS),
        node("Respond Error", "n8n-nodes-base.respondToWebhook", 1.1, (2420, 640), {
            "respondWith": "json", "responseBody": "={{ $json.response }}",
            "options": {"responseCode": "={{ $json.httpStatus }}"},
        }),
    ]

    def to(*targets):
        return {"main": [[{"node": t, "type": "main", "index": 0}] if t else [] for t in targets]}

    connections = {
        "Receive Inquiry": to("Set Config"),
        "Set Config": to("Validate Input"),
        "Validate Input": to("Input OK?"),
        "Input OK?": to("Build Prompt", "Build Input Error"),
        "Build Prompt": to("Call LLM"),
        "Call LLM": to("Validate LLM Output", "Capture LLM Error"),
        "Validate LLM Output": to("Output Valid?"),
        "Output Valid?": to("Route by Category", "Can Retry?"),
        "Route by Category": to(*[nm for _, nm, _, _ in ROUTES]),
        **{nm: to("Build Record") for _, nm, _, _ in ROUTES},
        "Build Record": to("Respond Success"),
        "Can Retry?": to("Prepare Retry", "Build Fallback Record"),
        "Build Input Error": to("Log Error"),
        "Capture LLM Error": to("Build Fallback Record"),
        "Build Fallback Record": to("Log Error"),
        "Log Error": to("Respond Error"),
        "Prepare Retry": to("Build Prompt"),
    }

    return {
        "name": "AI Inquiry Mail Triage (Portfolio)",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
        "meta": {"templateCredsSetupCompleted": False},
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
