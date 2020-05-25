#!/bin/sh
# tutorial https://www.digitalocean.com/community/tutorials/how-to-setup-dnssec-on-an-authoritative-bind-dns-server--2
# for zone debugging https://dnsviz.net/d/uciliste-sestine.hr/dnssec/
PDIR=`pwd`
# ZONEDIR="/root/LoadBalancer/bind/bind/lib" #location of your zone files
if [ $# -ne 2 ]; then
	echo "usage: $0 zone zonefile"
	exit 1
fi
ZONE=$1
ZONEFILE=$2
SALTFILE=$2.salt
if [ -r ${SALTFILE} ]; then
	SALT=`cat ${SALTFILE}`
else
	SALT=$(head -c 1000 /dev/random | sha1sum | cut -b 1-16)
	echo ${SALT} > ${SALTFILE}
fi
echo SALT=${SALT}

# cd $ZONEDIR
SERIAL=`named-checkzone $ZONE $ZONEFILE | egrep -ho '[0-9]{10}'`
# sed -i 's/'$SERIAL'/'$(($SERIAL+1))'/' $ZONEFILE
dnssec-signzone -A -3 ${SALT} -N date -o $ZONE -t $ZONEFILE
