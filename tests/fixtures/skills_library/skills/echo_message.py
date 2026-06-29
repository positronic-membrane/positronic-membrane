def run(message=""):
    sdk["logger"].info("echo_message called")
    return {"result": message}
