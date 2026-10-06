"""Opt-in live verification: three records and one WAV, no production writes."""
import asyncio
import json
import tempfile
from pathlib import Path

import aiohttp

from test_voice_safety import REPO, VM, data


async def main():
    names = ["\u963f\u7c73\u5a05", "\u739b\u6069\u7eb3", "\u4ee4"]
    async with aiohttp.ClientSession(headers=VM.DEFAULT_HEADERS,
                                     timeout=aiohttp.ClientTimeout(total=45, connect=10),
                                     trust_env=True) as session:
        records = await data.prts.fetch_voice_records(session, names)
        if set(records) != set(names):
            raise AssertionError("Some requested voice records are missing")
        with tempfile.TemporaryDirectory(prefix="mrfz-live-") as directory:
            manager = VM(Path(directory), REPO)
            for name, record in records.items():
                plan = manager.build_download_plan(name, record, True, "123456")
                assert plan and record["avatar_url"]
                assert len({(item["skin_directory"], item["language"], item["voice"])
                            for item in plan}) == len(plan)
                print(json.dumps({"character": name, "titles": len(record["files"]),
                                  "sources": len(record["sources"]), "slots": len(plan),
                                  "avatar": bool(record["avatar_url"])}, ensure_ascii=False))
            plan = manager.build_download_plan(names[1], records[names[1]], True, "2")
            distant = [item for item in plan if item["resource_id"] == "char_4064_mlynar_epoque__28"]
            assert len(distant) == 38, f"Distant-road skin has {len(distant)} planned slots"
            sample = distant[0]
            async with session.get(sample["url"]) as response:
                assert response.status == 200, f"Audio request HTTP {response.status}"
                payload = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    payload.extend(chunk)
                    assert len(payload) <= VM.MAX_VOICE_BYTES
                assert VM._looks_like_wav(payload), "Real upstream WAV failed structural validation"
            print(f"Live skin verification: 38 slots; valid WAV sample: {len(payload)} bytes")


if __name__ == "__main__":
    asyncio.run(main())
