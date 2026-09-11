FROM ghcr.io/berriai/litellm-database:v1.83.7-stable

COPY patch_license.py test_override.py /tmp/premium-override/
RUN python /tmp/premium-override/patch_license.py \
    && python /tmp/premium-override/test_override.py \
    && PYTHONPATH=/app python /tmp/premium-override/patch_license.py \
    && PYTHONPATH=/app python /tmp/premium-override/test_override.py \
    && rm -r /tmp/premium-override
