"""저장소: 로컬 시험용(LocalStore, sqlite)과 운영용(FirestoreStore)이 같은 메서드를 가진다.

컬렉션 구조는 docs/모바일앱_설계.md 3장.
  notices/{id}                    공고
  users/{uid}                     {profile, prefs}
  users/{uid}/devices/{token}     기기
  users/{uid}/inbox/{noticeId}    보낸(보낼) 알림
  outbox/{uid}__{noticeId}        조용한 시간이라 대기 중인 알림 (발송 후 삭제)
  system/{key}                    수집 상태
  cache/{key}                     지역코드·실거래 캐시
"""
import json
import sqlite3
from datetime import datetime


class LocalStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        for sql in (
            "CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, apply_end TEXT, doc TEXT)",
            "CREATE TABLE IF NOT EXISTS users (uid TEXT PRIMARY KEY, doc TEXT)",
            "CREATE TABLE IF NOT EXISTS devices (uid TEXT, token TEXT, doc TEXT, PRIMARY KEY (uid, token))",
            "CREATE TABLE IF NOT EXISTS inbox (uid TEXT, nid TEXT, doc TEXT, PRIMARY KEY (uid, nid))",
            "CREATE TABLE IF NOT EXISTS outbox (uid TEXT, nid TEXT, due TEXT, PRIMARY KEY (uid, nid))",
            "CREATE TABLE IF NOT EXISTS system (k TEXT PRIMARY KEY, doc TEXT)",
            "CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, at TEXT)",
        ):
            self.db.execute(sql)
        self.db.commit()

    def _one(self, sql, args):
        row = self.db.execute(sql, args).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, sql, args):
        self.db.execute(sql, args)
        self.db.commit()

    # 공고
    def get_notice(self, nid):
        return self._one("SELECT doc FROM notices WHERE id=?", (nid,))

    def put_notice(self, doc):
        self._put("INSERT OR REPLACE INTO notices VALUES (?, ?, ?)",
                  (doc["id"], doc["dates"].get("applyEnd") or "", json.dumps(doc, ensure_ascii=False)))

    def count_notices(self, prefix):
        return self.db.execute("SELECT COUNT(*) FROM notices WHERE id LIKE ?", (prefix + "%",)).fetchone()[0]

    def open_notices(self, today):
        """접수가 끝나지 않은 공고"""
        return [json.loads(r[0]) for r in self.db.execute("SELECT doc FROM notices WHERE apply_end >= ?", (today,))]

    def delete_notice(self, nid):  # 시험용
        self._put("DELETE FROM notices WHERE id=?", (nid,))

    # 사용자
    def list_users(self):
        return [(uid, json.loads(doc)) for uid, doc in self.db.execute("SELECT uid, doc FROM users")]

    def put_user(self, uid, doc):
        self._put("INSERT OR REPLACE INTO users VALUES (?, ?)", (uid, json.dumps(doc, ensure_ascii=False)))

    def devices(self, uid):
        return [t for (t,) in self.db.execute("SELECT token FROM devices WHERE uid=?", (uid,))]

    def put_device(self, uid, token, doc):
        self._put("INSERT OR REPLACE INTO devices VALUES (?, ?, ?)", (uid, token, json.dumps(doc)))

    def remove_device(self, uid, token):
        self._put("DELETE FROM devices WHERE uid=? AND token=?", (uid, token))

    # 알림함
    def inbox_exists(self, uid, nid):
        return self.db.execute("SELECT 1 FROM inbox WHERE uid=? AND nid=?", (uid, nid)).fetchone() is not None

    def put_inbox(self, uid, nid, doc):
        self._put("INSERT OR REPLACE INTO inbox VALUES (?, ?, ?)", (uid, nid, json.dumps(doc, ensure_ascii=False)))

    def get_inbox(self, uid, nid):
        return self._one("SELECT doc FROM inbox WHERE uid=? AND nid=?", (uid, nid))

    def list_inbox(self, uid):  # 시험용
        return [json.loads(d) for (d,) in self.db.execute("SELECT doc FROM inbox WHERE uid=?", (uid,))]

    def queue(self, uid, nid, due):
        self._put("INSERT OR REPLACE INTO outbox VALUES (?, ?, ?)", (uid, nid, due.isoformat()))

    def due_outbox(self, now):
        return list(self.db.execute("SELECT uid, nid FROM outbox WHERE due <= ?", (now.isoformat(),)))

    def unqueue(self, uid, nid):
        self._put("DELETE FROM outbox WHERE uid=? AND nid=?", (uid, nid))

    # 상태
    def get_state(self, k, default=None):
        v = self._one("SELECT doc FROM system WHERE k=?", (k,))
        return default if v is None else v

    def set_state(self, k, v):
        self._put("INSERT OR REPLACE INTO system VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))

    # 캐시 (price.PriceLookup이 쓴다)
    def get(self, k):
        row = self.db.execute("SELECT v, at FROM cache WHERE k=?", (k,)).fetchone()
        return (json.loads(row[0]), datetime.fromisoformat(row[1])) if row else None

    def set(self, k, v):
        self._put("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                  (k, json.dumps(v, ensure_ascii=False), datetime.now().isoformat()))


class FirestoreStore:
    """운영용. google-cloud-firestore 필요. Cloud Run에서는 서비스 계정으로 자동 인증된다."""

    def __init__(self, project=None):
        from google.cloud import firestore
        self.fs = firestore
        self.db = firestore.Client(project=project)

    def _doc(self, path):
        return self.db.document(path)

    @staticmethod
    def _key(k):
        return k.replace("/", "_")  # 문서 ID에는 '/'를 쓸 수 없다

    # 공고
    def get_notice(self, nid):
        snap = self._doc(f"notices/{nid}").get()
        return snap.to_dict() if snap.exists else None

    def put_notice(self, doc):
        self._doc(f"notices/{doc['id']}").set({**doc, "updatedAt": self.fs.SERVER_TIMESTAMP})

    def count_notices(self, prefix):
        q = self.db.collection("notices").where("id", ">=", prefix).where("id", "<", prefix + "").limit(1)
        return len(list(q.stream()))

    def open_notices(self, today):
        q = self.db.collection("notices").where("dates.applyEnd", ">=", today)
        return [s.to_dict() for s in q.stream()]

    # 사용자
    def list_users(self):
        return [(s.id, s.to_dict()) for s in self.db.collection("users").stream()]

    def put_user(self, uid, doc):
        self._doc(f"users/{uid}").set(doc, merge=True)

    def devices(self, uid):
        return [s.id for s in self.db.collection(f"users/{uid}/devices").stream()]

    def put_device(self, uid, token, doc):
        self._doc(f"users/{uid}/devices/{token}").set(doc)

    def remove_device(self, uid, token):
        self._doc(f"users/{uid}/devices/{token}").delete()

    # 알림함
    def inbox_exists(self, uid, nid):
        return self._doc(f"users/{uid}/inbox/{nid}").get().exists

    def put_inbox(self, uid, nid, doc):
        self._doc(f"users/{uid}/inbox/{nid}").set(doc, merge=True)

    def get_inbox(self, uid, nid):
        snap = self._doc(f"users/{uid}/inbox/{nid}").get()
        return snap.to_dict() if snap.exists else None

    def queue(self, uid, nid, due):
        self._doc(f"outbox/{uid}__{nid}").set({"uid": uid, "nid": nid, "due": due.isoformat()})

    def due_outbox(self, now):
        q = self.db.collection("outbox").where("due", "<=", now.isoformat())
        return [(s.get("uid"), s.get("nid")) for s in q.stream()]

    def unqueue(self, uid, nid):
        self._doc(f"outbox/{uid}__{nid}").delete()

    # 상태
    def get_state(self, k, default=None):
        snap = self._doc(f"system/{k}").get()
        return snap.to_dict().get("v", default) if snap.exists else default

    def set_state(self, k, v):
        self._doc(f"system/{k}").set({"v": v})

    # 캐시
    def get(self, k):
        snap = self._doc(f"cache/{self._key(k)}").get()
        if not snap.exists:
            return None
        d = snap.to_dict()
        return json.loads(d["v"]), datetime.fromisoformat(d["at"])

    def set(self, k, v):
        # 실거래 목록은 배열이 커서 JSON 문자열로 저장 (문서 1MB 제한 안)
        self._doc(f"cache/{self._key(k)}").set({"v": json.dumps(v, ensure_ascii=False),
                                                "at": datetime.now().isoformat()})
