#include "tju_tcp.h"
#include <string.h>


int main(int argc, char **argv) {
    // Start the simulation environment
    startSimulation();

    tju_tcp_t* my_socket = tju_socket();
    
    tju_sock_addr target_addr;
    target_addr.ip = inet_network(SERVER_IP);
    target_addr.port = 1234;

    tju_connect(my_socket, target_addr);

    

    return EXIT_SUCCESS;
}
