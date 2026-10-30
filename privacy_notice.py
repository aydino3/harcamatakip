"""Versioned privacy information and authenticated acknowledgments, not consent."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

import app as core

ACKNOWLEDGMENT = {
    'tr': 'KVKK Aydınlatma Metni’ni okudum ve kişisel verilerimin işlenmesi hakkında bilgi edindim.',
    'en': 'I have read the Privacy Notice and received information about the processing of my personal data.',
}


class NoticeError(ValueError):
    status = 428
    code = 'privacy_acknowledgment_required'


class StaleNotice(NoticeError):
    status = 409
    code = 'privacy_notice_changed'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def load_notice(value, allow_draft=False):
    """Only an operator-supplied, complete document can enable the auth gate."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('Aydınlatma metni yapılandırması geçersiz.')
    if value.get('publication_ready') is not True and not allow_draft:
        raise ValueError('Aydınlatma metninin hukuki eksikleri tamamlanmadan etkinleştirilemez.')
    version = value.get('version')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', version):
        raise ValueError('Aydınlatma metni sürümü geçersiz.')
    controller = value.get('controller', {})
    if not isinstance(controller, dict) or any(not isinstance(controller.get(key), str) or not controller[key].strip() for key in ('name', 'email')):
        raise ValueError('Veri sorumlusunun adı ve başvuru e-postası eksik.')
    if controller['email'].count('@') != 1 or any(c.isspace() for c in controller['email']):
        raise ValueError('KVKK başvuru e-postası geçersiz.')
    languages = value.get('languages')
    if not isinstance(languages, dict) or 'tr' not in languages or set(languages) - set(ACKNOWLEDGMENT):
        raise ValueError('Aydınlatma metninin dili geçersiz.')
    documents = {}
    for language, content in languages.items():
        if not isinstance(content, dict) or not isinstance(content.get('title'), str) or not content['title'].strip():
            raise ValueError('Aydınlatma metninin başlığı eksik.')
        sections = content.get('sections')
        required = {'controller', 'data_and_purposes', 'legal_basis_and_collection', 'recipients_and_transfers', 'retention', 'rights_and_application'}
        if not isinstance(sections, list) or {row.get('key') for row in sections if isinstance(row, dict)} != required or len(sections) != len(required):
            raise ValueError('Aydınlatma metninin zorunlu açıklamaları eksik.')
        if any(not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key].strip() for key in ('title', 'text')) for row in sections):
            raise ValueError('Aydınlatma metninde boş açıklama var.')
        document = {'version': version, 'language': language, 'controller': controller, 'title': content['title'], 'sections': sections, 'acknowledgment': ACKNOWLEDGMENT[language]}
        serialized = canonical(document)
        documents[language] = {**document, 'sha256': hashlib.sha256(serialized.encode()).hexdigest()}
    return {'enabled': True, 'draft': value.get('publication_ready') is not True, 'version': version, 'documents': documents}


def public_notice(notice):
    return notice if notice else {'enabled': False}


def from_file(path):
    with open(path, encoding='utf-8') as handle:
        return load_notice(json.load(handle))


def initialize(notice):
    """Additive schema; no fabricated acknowledgments for existing accounts."""
    with core.connect() as db:
        pg = hasattr(db, '_conn')
        integer = 'BIGINT' if pg else 'INTEGER'
        db.execute('''CREATE TABLE IF NOT EXISTS privacy_notice_versions (
            version TEXT NOT NULL, language TEXT NOT NULL CHECK(language IN ('tr','en')),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64), document_json TEXT NOT NULL,
            published_at TEXT NOT NULL, PRIMARY KEY(version,language), UNIQUE(version,language,sha256))''')
        db.execute(f'''CREATE TABLE IF NOT EXISTS privacy_acknowledgments (
            id TEXT PRIMARY KEY, user_id {integer} NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL CHECK(action IN ('register','login')),
            version TEXT NOT NULL, language TEXT NOT NULL, notice_sha256 TEXT NOT NULL,
            declaration TEXT NOT NULL CHECK(declaration='notice_read_and_informed'),
            acknowledgment_text TEXT NOT NULL, acknowledged_at TEXT NOT NULL,
            session_token_hash TEXT UNIQUE,
            FOREIGN KEY(version,language,notice_sha256) REFERENCES privacy_notice_versions(version,language,sha256))''')
        db.execute('CREATE INDEX IF NOT EXISTS privacy_ack_owner_time ON privacy_acknowledgments(user_id,acknowledged_at)')
        if pg:
            for table in ('privacy_notice_versions', 'privacy_acknowledgments'):
                db.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
                db.execute(f'REVOKE ALL ON {table} FROM PUBLIC')
                for role in ('anon', 'authenticated'):
                    if db.execute('SELECT 1 FROM pg_roles WHERE rolname=?', (role,)).fetchone():
                        db.execute(f'REVOKE ALL ON {table} FROM "{role}"')
        for document in notice['documents'].values():
            publish(db, document)


def publish(db, document):
    content = {key: value for key, value in document.items() if key != 'sha256'}
    text = canonical(content)
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest != document['sha256']:
        raise ValueError('Aydınlatma metninin içerik özeti tutarsız.')
    db.execute('''INSERT INTO privacy_notice_versions(version,language,sha256,document_json,published_at)
        VALUES(?,?,?,?,?) ON CONFLICT(version,language) DO NOTHING''',
        (document['version'], document['language'], digest, text, datetime.now(timezone.utc).isoformat()))
    row = db.execute('SELECT sha256,document_json FROM privacy_notice_versions WHERE version=? AND language=?', (document['version'], document['language'])).fetchone()
    if row['sha256'] != digest or row['document_json'] != text:
        raise ValueError('Yayımlanmış aydınlatma metni değiştirilemez; yeni sürüm kullanın.')


@dataclass(frozen=True)
class Acknowledgment:
    document_json: str
    sha256: str
    action: str


def prepare_acknowledgment(notice, payload, action):
    if not notice:
        return None
    if action not in ('register', 'login'):
        raise ValueError('Beyan işlem türü geçersiz.')
    if not isinstance(payload, dict) or payload.get('read') is not True:
        raise NoticeError('Devam etmek için aydınlatma metnini okuyup bilgi edindiğinizi belirtin.')
    language = payload.get('language')
    document = notice['documents'].get(language) if isinstance(language, str) else None
    if not document or payload.get('version') != document['version'] or payload.get('sha256') != document['sha256']:
        raise StaleNotice('Aydınlatma metni güncellendi. Güncel metni açıp yeniden bilgi edinin.')
    return Acknowledgment(canonical({key: val for key, val in document.items() if key != 'sha256'}), document['sha256'], action)


def record_acknowledgment(db, user_id, acknowledgment, session_token_hash=None):
    if not isinstance(acknowledgment, Acknowledgment):
        raise ValueError('Geçersiz aydınlatma beyanı.')
    document = json.loads(acknowledgment.document_json)
    if hashlib.sha256(acknowledgment.document_json.encode()).hexdigest() != acknowledgment.sha256:
        raise ValueError('Aydınlatma beyanının metin özeti tutarsız.')
    publish(db, {**document, 'sha256': acknowledgment.sha256})
    receipt = str(uuid.uuid4())
    db.execute('''INSERT INTO privacy_acknowledgments
        (id,user_id,action,version,language,notice_sha256,declaration,acknowledgment_text,acknowledged_at,session_token_hash)
        VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (receipt, user_id, acknowledgment.action, document['version'], document['language'], acknowledgment.sha256,
         'notice_read_and_informed', document['acknowledgment'], datetime.now(timezone.utc).isoformat(), session_token_hash))
    return receipt


def acknowledgments_for_user(user_id):
    with core.connect() as db:
        return [dict(row) for row in db.execute('''SELECT id,action,version,language,notice_sha256,declaration,acknowledgment_text,acknowledged_at
            FROM privacy_acknowledgments WHERE user_id=? ORDER BY acknowledged_at DESC,id''', (user_id,))]
