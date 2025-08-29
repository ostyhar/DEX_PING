#! /bin/bash

# sudo mount -t vboxsf Python /home/os/Python

aws ecr get-login-password --region eu-west-1 | docker login --username AWS --password-stdin 156807677375.dkr.ecr.eu-west-1.amazonaws.com

docker build -t dex-ping-lambda:app .

docker tag dex-ping-lambda:app 156807677375.dkr.ecr.eu-west-1.amazonaws.com/dex-ping-lambda:app

docker container prune -f

if [[ $(docker images -q --filter "dangling=true") ]]; then
    docker rmi $(docker images -q --filter "dangling=true")
fi

docker push 156807677375.dkr.ecr.eu-west-1.amazonaws.com/dex-ping-lambda:app

# dex-ping-lambda

aws lambda update-function-code --function-name dex-ping-lambda --region eu-west-1 --profile default --image-uri 156807677375.dkr.ecr.eu-west-1.amazonaws.com/dex-ping-lambda:app
