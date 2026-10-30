"""A parked editor cannot be reused for a different document owner."""
from __future__ import annotations

import time
import uuid
import json


async def rename_and_recreate_source(suite, browser):
    source, destination = "notes.py", "renamed.py"
    original = "# original\n"
    context, page, channel, cell, errors = await suite.fixture(browser, source, original)
    requests = []
    page.on("request", lambda request: requests.append({"url": request.url, "body": request.post_data})
            if "/files/mutate" in request.url else None)
    try:
        await page.get_by_role("button", name="Show files", exact=True).last.click()
        suite.fault(hold_before=True)
        frame, _, suffix = await suite.type_suffix(page, "monaco", suffix=" original-owner-has-many-edits")
        old_element = await frame.frame_element()
        await old_element.evaluate("element => element.dataset.persistenceOwnerProbe = 'old-source'")

        row = page.locator(f'[data-filenode-path="{source}"]')
        await row.hover()
        await row.get_by_title("Rename", exact=True).click()
        field = row.locator("input")
        await field.fill(destination)
        await field.press("Enter")
        dialog = page.get_by_role("dialog").filter(has=page.get_by_role(
            "heading", name="Save your edits and rename?", exact=True))
        await dialog.get_by_role("button", name="Save and rename", exact=True).click()
        suite.fault()

        deadline = time.monotonic() + 15
        while True:
            inventory = suite.api("GET", f"/api/channels/{channel}/cells/{cell}/files")["files"]
            tombstone = next(file for file in inventory if file["path"] == source)
            target = next((file for file in inventory if file["path"] == destination), None)
            if tombstone["deleted"] and target and not target["deleted"]:
                break
            assert time.monotonic() < deadline, "Dirty rename did not finish"
            await page.wait_for_timeout(50)
        expected_destination = original + suffix
        assert suite.file(channel, cell, destination) == expected_destination
        original_writer = next(json.loads(request['body'])['mutations'][0]['writer_id']
                               for request in requests
                               if len(json.loads(request['body'])['mutations']) == 1
                               and json.loads(request['body'])['mutations'][0]['path'] == source)
        await suite.open_file(page, destination)
        await suite.editor(page, "monaco", destination)

        replacement = "# a different document\n"
        receipt = suite.api("POST", f"/api/channels/{channel}/cells/{cell}/files/mutate", {
            "operation_id": str(uuid.uuid4()),
            "mutations": [{"path": source, "base_revision": tombstone["file_revision"],
                           "writer_id": "torture:recreated-source", "edit_seq": 1,
                           "kind": "put", "content_text": replacement, "language": "python"}],
        })
        assert receipt["outcome"] == "accepted"
        await suite.open_file(page, source)
        reopened_frame, reopened_editor = await suite.editor(page, "monaco", source)
        element = await reopened_frame.frame_element()
        assert await element.get_attribute("data-persistence-owner-probe") is None, (
            "A parked view bound to the renamed owner was reused for the recreated source")
        await reopened_frame.wait_for_function(
            "expected => window.monaco?.editor.getModels()[0]?.getValue() === expected",
            arg=replacement, timeout=10000)
        await reopened_editor.focus()
        await reopened_editor.press("Control+End")
        await reopened_editor.press_sequentially("z", delay=35)
        expected_source = replacement + "z"
        deadline = time.monotonic() + 10
        while suite.file(channel, cell, source) != expected_source:
            assert time.monotonic() < deadline, "Recreated source did not save its new owner's bytes"
            await page.wait_for_timeout(50)
        assert suite.file(channel, cell, destination) == expected_destination, (
            "Editing the recreated source changed the renamed document")
        accepted = next(file for file in suite.api(
            'GET', f'/api/channels/{channel}/cells/{cell}/files')['files'] if file['path'] == source)
        assert accepted['writer_id'] != original_writer, 'Recreated source reused the prior sequence domain'
        return {"renderer": "monaco", "host_role": "pooled",
                "dirty_owner_followed_rename": True, "source_recreated_at_tombstone": True,
                "new_physical_view_for_new_owner": True, "independent_bytes_preserved": True,
                "single_new_edit_accepted": True,
                "page_errors": errors}
    finally:
        try:
            evidence = {"requests": requests, "body": await page.locator("body").inner_text(),
                        "source_journal": await suite.journal(page, source),
                        "destination_journal": await suite.journal(page, destination),
                        "inventory": suite.api("GET", f"/api/channels/{channel}/cells/{cell}/files"),
                        "page_errors": errors}
            (suite.directory / "rename-recreate-evidence.json").write_text(json.dumps(evidence, indent=2))
        finally:
            suite.fault()
            await context.close()
