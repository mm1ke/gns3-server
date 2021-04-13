#!/usr/bin/env python
#
# Copyright (C) 2020 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import pytest

from typing import Optional
from fastapi import FastAPI, HTTPException, status
from httpx import AsyncClient
from jose import jwt

from sqlalchemy.ext.asyncio import AsyncSession
from gns3server.db.repositories.users import UsersRepository
from gns3server.services import auth_service
from gns3server.services.authentication import DEFAULT_JWT_SECRET_KEY
from gns3server.config import Config
from gns3server.schemas.users import User

pytestmark = pytest.mark.asyncio


class TestUserRoutes:

    async def test_route_exist(self, app: FastAPI, client: AsyncClient) -> None:

        new_user = {"username": "user1", "email": "user1@email.com", "password": "test_password"}
        response = await client.post(app.url_path_for("create_user"), json=new_user)
        assert response.status_code != status.HTTP_404_NOT_FOUND

    async def test_users_can_register_successfully(
            self,
            app: FastAPI,
            client: AsyncClient,
            db_session: AsyncSession
    ) -> None:

        user_repo = UsersRepository(db_session)
        params = {"username": "user2", "email": "user2@email.com", "password": "test_password"}

        # make sure the user doesn't exist in the database
        user_in_db = await user_repo.get_user_by_username(params["username"])
        assert user_in_db is None

        # register the user
        res = await client.post(app.url_path_for("create_user"), json=params)
        assert res.status_code == status.HTTP_201_CREATED

        # make sure the user does exists in the database now
        user_in_db = await user_repo.get_user_by_username(params["username"])
        assert user_in_db is not None
        assert user_in_db.email == params["email"]
        assert user_in_db.username == params["username"]

        # check that the user returned in the response is equal to the user in the database
        created_user = User(**res.json()).json()
        assert created_user == User.from_orm(user_in_db).json()

    @pytest.mark.parametrize(
        "attr, value, status_code",
        (
                ("email", "user2@email.com", status.HTTP_400_BAD_REQUEST),
                ("username", "user2", status.HTTP_400_BAD_REQUEST),
                ("email", "invalid_email@one@two.io", status.HTTP_422_UNPROCESSABLE_ENTITY),
                ("password", "short", status.HTTP_422_UNPROCESSABLE_ENTITY),
                ("username", "user2@#$%^<>", status.HTTP_422_UNPROCESSABLE_ENTITY),
                ("username", "ab", status.HTTP_422_UNPROCESSABLE_ENTITY),
        )
    )
    async def test_user_registration_fails_when_credentials_are_taken(
            self,
            app: FastAPI,
            client: AsyncClient,
            attr: str,
            value: str,
            status_code: int,
    ) -> None:

        new_user = {"email": "not_taken@email.com", "username": "not_taken_username", "password": "test_password"}
        new_user[attr] = value
        res = await client.post(app.url_path_for("create_user"), json=new_user)
        assert res.status_code == status_code

    async def test_users_saved_password_is_hashed(
        self,
        app: FastAPI,
        client: AsyncClient,
        db_session: AsyncSession
    ) -> None:

        user_repo = UsersRepository(db_session)
        new_user = {"username": "user3", "email": "user3@email.com", "password": "test_password"}

        # send post request to create user and ensure it is successful
        res = await client.post(app.url_path_for("create_user"), json=new_user)
        assert res.status_code == status.HTTP_201_CREATED

        # ensure that the users password is hashed in the db
        # and that we can verify it using our auth service
        user_in_db = await user_repo.get_user_by_username(new_user["username"])
        assert user_in_db is not None
        assert user_in_db.hashed_password != new_user["password"]
        assert auth_service.verify_password(new_user["password"], user_in_db.hashed_password)

    async def test_get_users(self, app: FastAPI, client: AsyncClient) -> None:

        response = await client.get(app.url_path_for("get_users"))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()) == 3  # user1, user2 and user3 should exist


class TestAuthTokens:

    async def test_can_create_token_successfully(
            self,
            app: FastAPI,
            client: AsyncClient,
            test_user: User,
            config: Config
    ) -> None:

        jwt_secret = config.settings.Controller.jwt_secret_key
        token = auth_service.create_access_token(test_user.username)
        payload = jwt.decode(token, jwt_secret, algorithms=["HS256"])
        username = payload.get("sub")
        assert username == test_user.username

    async def test_token_missing_user_is_invalid(self, app: FastAPI, client: AsyncClient, config: Config) -> None:

        jwt_secret = config.settings.Controller.jwt_secret_key
        token = auth_service.create_access_token(None)
        with pytest.raises(jwt.JWTError):
            jwt.decode(token, jwt_secret, algorithms=["HS256"])

    async def test_can_retrieve_username_from_token(
            self,
            app: FastAPI,
            client: AsyncClient,
            test_user: User
    ) -> None:

        token = auth_service.create_access_token(test_user.username)
        username = auth_service.get_username_from_token(token)
        assert username == test_user.username


    @pytest.mark.parametrize(
        "wrong_secret, wrong_token",
        (
                ("use correct secret", "asdf"),  # use wrong token
                ("use correct secret", ""),  # use wrong token
                ("ABC123", "use correct token"),  # use wrong secret
        ),
    )
    async def test_error_when_token_or_secret_is_wrong(
            self,
            app: FastAPI,
            client: AsyncClient,
            test_user: User,
            wrong_secret: str,
            wrong_token: Optional[str],
            config,
    ) -> None:

        token = auth_service.create_access_token(test_user.username)
        if wrong_secret == "use correct secret":
            wrong_secret = config.settings.Controller.jwt_secret_key
        if wrong_token == "use correct token":
            wrong_token = token
        with pytest.raises(HTTPException):
            auth_service.get_username_from_token(wrong_token, secret_key=wrong_secret)


class TestUserLogin:

    async def test_user_can_login_successfully_and_receives_valid_token(
            self,
            app: FastAPI,
            client: AsyncClient,
            test_user: User,
            config: Config
    ) -> None:

        jwt_secret = config.settings.Controller.jwt_secret_key
        client.headers["content-type"] = "application/x-www-form-urlencoded"
        login_data = {
            "username": test_user.username,
            "password": "user1_password",
        }
        res = await client.post(app.url_path_for("login"), data=login_data)
        assert res.status_code == status.HTTP_200_OK

        # check that token exists in response and has user encoded within it
        token = res.json().get("access_token")
        payload = jwt.decode(token, jwt_secret, algorithms=["HS256"])
        assert "sub" in payload
        username = payload.get("sub")
        assert username == test_user.username

        # check that token is proper type
        assert "token_type" in res.json()
        assert res.json().get("token_type") == "bearer"

    @pytest.mark.parametrize(
        "username, password, status_code",
        (
            ("wrong_username", "user1_password", status.HTTP_401_UNAUTHORIZED),
            ("user1", "wrong_password", status.HTTP_401_UNAUTHORIZED),
            ("user1", None, status.HTTP_401_UNAUTHORIZED),
        ),
    )
    async def test_user_with_wrong_creds_doesnt_receive_token(
        self,
        app: FastAPI,
        client: AsyncClient,
        test_user: User,
        username: str,
        password: str,
        status_code: int,
    ) -> None:

        client.headers["content-type"] = "application/x-www-form-urlencoded"
        login_data = {
            "username": username,
            "password": password,
        }
        res = await client.post(app.url_path_for("login"), data=login_data)
        assert res.status_code == status_code
        assert "access_token" not in res.json()


class TestUserMe:

    async def test_authenticated_user_can_retrieve_own_data(
            self,
            app: FastAPI,
            authorized_client: AsyncClient,
            test_user: User,
    ) -> None:

        res = await authorized_client.get(app.url_path_for("get_current_active_user"))
        assert res.status_code == status.HTTP_200_OK
        user = User(**res.json())
        assert user.username == test_user.username
        assert user.email == test_user.email
        assert user.user_id == test_user.user_id

    async def test_user_cannot_access_own_data_if_not_authenticated(
            self, app: FastAPI,
            client: AsyncClient,
            test_user: User,
    ) -> None:

        res = await client.get(app.url_path_for("get_current_active_user"))
        assert res.status_code == status.HTTP_401_UNAUTHORIZED