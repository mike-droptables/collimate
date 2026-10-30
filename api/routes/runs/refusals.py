"""Run-start failures with one internal type and an unchanged HTTP body."""


class RunRefused(Exception):
    def __init__(self, status_code: int, body: dict, *, headers=None):
        self.status_code = status_code
        self.body = body
        self.headers = headers
        fields = [field.get("label") or field.get("key") for field in body.get("fields", [])
                  if isinstance(field, dict)]
        if body.get("code") == "missing_required" and fields:
            reason = f"required settings are empty: {', '.join(str(field) for field in fields)}"
        elif body.get("code") == "config_invalid":
            reason = "a saved setting selects an option that is not available"
        else:
            reason = str(body.get("detail") or body.get("code") or "the run was refused")
        super().__init__(reason)
