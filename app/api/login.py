from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pymongo.collection import Collection
from uuid import uuid4

from app.api.deps import get_customer_collection, get_user_collection
from app.core.access_profile import build_user_public
from app.core.security import create_access_token, verify_password
from app.core.config import get_settings
from app.models.user import AuthResponse, LoginRequest, UserInDB

router = APIRouter(prefix="/login", tags=["login"])
bearer_scheme = HTTPBearer(auto_error=False)


@router.post("", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    collection: Collection = Depends(get_user_collection),
    customer_collection: Collection = Depends(get_customer_collection),
) -> AuthResponse:
    identifier = payload.identifier.strip().lower()
    document = collection.find_one(
        {
            "$or": [
                {"email": identifier},
                {"phone_number": payload.identifier.strip()},
            ]
        }
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    user = UserInDB.from_mongo(document)
    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    new_session_id = str(uuid4())
    collection.update_one({"_id": user.id}, {"$set": {"session_id": new_session_id}})

    token = create_access_token(user.id, new_session_id)
    return AuthResponse(
        access_token=token,
        user=build_user_public(user, collection, customer_collection),
    )


@router.post("/refresh-session", response_model=AuthResponse)
def refresh_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    collection: Collection = Depends(get_user_collection),
    customer_collection: Collection = Depends(get_customer_collection),
) -> AuthResponse:
    """Issue a new session/token even if the current token just expired."""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
        subject = payload.get("sub")
        session_id = payload.get("jti")
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token") from exc

    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")

    document = collection.find_one({"_id": subject})
    if document is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if document.get("session_id") != session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired. Please log in again.")

    new_session_id = str(uuid4())
    collection.update_one({"_id": subject}, {"$set": {"session_id": new_session_id}})
    refreshed = collection.find_one({"_id": subject}) or document
    user = UserInDB.from_mongo(refreshed)
    token = create_access_token(user.id, new_session_id)
    return AuthResponse(
        access_token=token,
        user=build_user_public(user, collection, customer_collection),
    )