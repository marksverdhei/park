FROM docker:cli
RUN apk update \
  && apk add github-cli \
  && apk add python3 

COPY main.py main.py

CMD ["python3", "main.py"]
