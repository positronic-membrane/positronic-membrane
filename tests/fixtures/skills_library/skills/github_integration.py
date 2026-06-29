def run(action, repo, **kwargs):
    gh = sdk["github"]
    if action == "list_open_issues":
        return gh.list_open_issues(repo, label=kwargs.get("label"))
    if action == "get_issue":
        return gh.get_issue(repo, kwargs["number"])
    if action == "create_issue":
        return gh.create_issue(repo, kwargs["title"], kwargs.get("body", ""), kwargs.get("labels"))
    if action == "add_comment":
        return gh.add_comment(repo, kwargs["number"], kwargs["body"])
    if action == "close_issue":
        return gh.close_issue(repo, kwargs["number"])
    if action == "create_pr":
        return gh.create_pr(
            repo, kwargs["title"], kwargs.get("body", ""), kwargs["head"], kwargs.get("base", "main")
        )
    return {"error": f"Unknown action: {action}"}
