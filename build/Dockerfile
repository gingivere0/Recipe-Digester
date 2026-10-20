# Native ARMv7 build environment (runs under Docker's QEMU emulation).
# Everything shipped to the Nook comes from this image, so it matches the
# Nook's CPU (Cortex-A8, hard-float) and the musl C library.
FROM --platform=linux/arm/v7 alpine:3.20

RUN apk add --no-cache \
        build-base linux-headers \
        busybox-static \
        wpa_supplicant curl ca-certificates \
        dropbear \
        font-dejavu \
        u-boot-tools cpio

WORKDIR /src
