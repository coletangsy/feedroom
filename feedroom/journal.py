"""Persist one publishing journal per account with conditional writes."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from time import sleep


class JournalError(RuntimeError):
    pass


class LocalJournal:
    def __init__(self, path: Path, account: str):
        self.path = path
        self.account = account
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS journals (account TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)")

    def read(self) -> tuple[dict, str | None]:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT body, revision FROM journals WHERE account = ?", (self.account,)).fetchone()
        return (json.loads(row[0]), str(row[1])) if row else ({}, None)

    def write(self, posts: dict, revision: str | None) -> bool:
        body = json.dumps(posts, ensure_ascii=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            if revision is None:
                cursor = db.execute("INSERT INTO journals VALUES (?, 1, ?) ON CONFLICT(account) DO NOTHING", (self.account, body))
            else:
                cursor = db.execute("UPDATE journals SET body = ?, revision = revision + 1 WHERE account = ? AND revision = ?", (body, self.account, revision))
            return cursor.rowcount == 1


class R2Journal:
    def __init__(self, client, bucket: str, account: str):
        self.client = client
        self.bucket = bucket
        self.key = f"threads/{account}.json"

    def read(self) -> tuple[dict, str | None]:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            result = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except ClientError as error:
            if error.response["Error"]["Code"] != "NoSuchKey":
                raise JournalError("Could not read the R2 publishing journal.") from None
            return {}, None
        except BotoCoreError:
            raise JournalError("Could not read the R2 publishing journal.") from None
        with closing(result["Body"]) as body:
            return json.load(body), result["ETag"]

    def write(self, posts: dict, revision: str | None) -> bool:
        from botocore.exceptions import BotoCoreError, ClientError

        condition = {"IfNoneMatch": "*"} if revision is None else {"IfMatch": revision}
        body = json.dumps(posts, ensure_ascii=False).encode("utf-8")
        for attempt in range(3):
            try:
                self.client.put_object(Bucket=self.bucket, Key=self.key, ContentType="application/json", Body=body, **condition)
                return True
            except ClientError as error:
                code = error.response["ResponseMetadata"]["HTTPStatusCode"]
                if code in {409, 412}:
                    return False
                if code != 429 or attempt == 2:
                    raise JournalError("Could not write the R2 publishing journal.") from None
                sleep(1 + attempt)
            except BotoCoreError:
                raise JournalError("Could not write the R2 publishing journal.") from None
