from fastapi import APIRouter, Depends, Request

router = APIRouter(prefix="/mcp", tags=["mcp"])


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


@router.get("")
async def mcp_get(
    _: None = Depends(auth_dependency),
) -> dict[str, str]:
    return {"status": "ok"}


@router.post("")
async def mcp_post(
    _: None = Depends(auth_dependency),
) -> dict[str, str]:
    return {"status": "ok"}
