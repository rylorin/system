# system stack

## About this project

This project contains all my docker swarm core services & utility commands.

## Services

These services run on a docker swarm, deployed in a "system" stack

### bind

Built on sameersbn/bind docker image.

### smtp

Built on rylorin/postfix-relay docker image.

### websites 

Reverse proxy and http static files server.

## Commands

`backup_volumes` backup a docker volume to local file system.

`download` copy files from remote server to local file system.

`install` install stack ie load config files, build local docker volumes, setup docker volumes over NFS, deploy stack.

`postintall` specific postinstallation tasks, called from install script.

`upload` copy files from local file system to remote server.

## Hierarchy

All my docker stacks have a common structure:

`bin` contain stack specific scripts ie postinstall.

`config` docker config files.

`volumes` a local docker volume will be created with each subdirectory content.

`volumes_nfs` files will be shared using NFS (must be configured and running on host) and wrapped in a "local" docker volume.

`docker-stack.yml` docker stack deploy specification.