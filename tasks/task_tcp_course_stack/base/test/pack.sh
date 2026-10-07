#!/bin/bash
pack_tju_tcp(){
    echo 'Packaging now. Packaging will run make clean to remove all build outputs'
    echo ''
    cd /vagrant/tju_tcp && make clean
    cd /vagrant && zip handin.zip ./tju_tcp/src ./tju_tcp/src/* ./tju_tcp/inc ./tju_tcp/inc/* ./tju_tcp/build ./tju_tcp/build/* ./tju_tcp/Makefile
    mv /vagrant/handin.zip /vagrant/tju_tcp
    echo ''
    echo 'Packaging complete. Please upload /vagrant/tju_tcp/handin.zip to the autograding website'
}


if command -v zip; then 
    pack_tju_tcp
else 
    echo 'zip is not installed; trying to install it'
    sudo apt install zip -y 
    pack_tju_tcp
fi