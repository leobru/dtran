#!/bin/sh
rm -f object.bin
cat << EOF > tmp$$
*NAME dms
*disc:1/local
*file:object,67,w
*call setftn:one,long
*assem
EOF
cat "$1" >> tmp$$
cat << EOF >>tmp$$
*to perso:670000
*call plcatalog
*end file
EOF
ulimit -t 5
rm -f object.o
if [ "$1" = "-d" ]; then ln -f tmp$$ asm.dub ; fi
length=`dubna tmp$$ | tee asm.lst | grep 'HA LIBRARY' | cut -d ' ' -f 5`
length=$(($length-2))
grep -q CBOБOДHO asm.lst
if [ $? -ne 0 ]; then 
echo '[1;31mFAILURE[22;39m'
fi
echo Module length is $length zones
dd bs=6k skip=2 count=$length < object.bin > object.o
rm -f tmp$$
