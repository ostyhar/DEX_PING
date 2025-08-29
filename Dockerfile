    FROM public.ecr.aws/lambda/python:3.12 AS build

    # Tools to build wheels
    RUN dnf install -y gcc python3-devel && \
        python -m pip install --upgrade pip
    
    # Install deps into /opt/python (Lambda layer-style path on PYTHONPATH)
    COPY requirements.txt .
    RUN pip install --no-cache-dir --target /opt/python -r requirements.txt
    
    # ---- final stage: slim runtime image ----
    FROM public.ecr.aws/lambda/python:3.12
    
    # Bring in the built site-packages
    COPY --from=build /opt/python /opt/python
    
    # Your function code
    COPY ./src/* ./
    
    # Lambda entrypoint
    CMD ["app.lambda_handler"]
    