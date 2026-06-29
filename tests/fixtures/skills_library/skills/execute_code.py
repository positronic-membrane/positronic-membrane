def execute_code(code):
    import re
    code = re.sub(r"^```python\s*", "", code, flags=re.IGNORECASE)
    code = re.sub(r"\s*```$", "", code, flags=re.IGNORECASE)
    return sdk['sandbox'].execute(code)
