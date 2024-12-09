#!/bin/bash

docker build -t tg-polyglot .
docker run -d --name telegram_bot_container --env-file .env tg-polyglot

