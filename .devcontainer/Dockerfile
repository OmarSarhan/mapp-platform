FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm@sha256:7876580dc67fd460fd962f004cbeb480027e9bbc0657096f1087db11f9eaff39

# The only reason this file exists.
#
# The image ships an apt source for yarn whose signing key is no longer
# available, so every apt-get update inside it ends with
#
#   NO_PUBKEY 62D54FD4003F6525
#   E: The repository 'https://dl.yarnpkg.com/debian stable InRelease' is not signed.
#
# and returns a failure. That breaks any devcontainer feature which installs
# packages -- docker-in-docker among them, which is the one feature this
# container needs. Nothing here uses yarn.
#
# Do not delete this without checking that `apt-get update` succeeds in the
# base image again. It is a workaround for the image, not for MAPP.
RUN rm -f /etc/apt/sources.list.d/yarn.list
