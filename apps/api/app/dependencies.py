from fastapi import Request


def get_run_service(request: Request):
    return request.app.state.run_service
