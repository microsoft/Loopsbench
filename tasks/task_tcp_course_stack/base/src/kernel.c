#include "kernel.h"
/*
Handler that simulates the Linux kernel receiving a TCP packet
*/
void onTCPPocket(char* pkt){
    // When we receive a TCP packet, the source IP and source port in the packet belong to the sender, which are the remote IP and port from our perspective
    uint16_t remote_port = get_src(pkt);
    uint16_t local_port = get_dst(pkt);
    // The remote IP and local IP would be read from the IP packet; in the simulation, determine them directly from the hostname

    char hostname[8];
    gethostname(hostname, 8);
    uint32_t remote_ip, local_ip;
    if(strcmp(hostname,"server")==0){ // This host is the server, so the remote endpoint is the client
        local_ip = inet_network(SERVER_IP);
        remote_ip = inet_network(CLIENT_IP);
    }else if(strcmp(hostname,"client")==0){ // This host is the client, so the remote endpoint is the server
        local_ip = inet_network(CLIENT_IP);
        remote_ip = inet_network(SERVER_IP);
    }

    int hashval;
    // Use the four-tuple formed by the two IPs and ports to find an established socket
    hashval = cal_hash(local_ip, local_port, remote_ip, remote_port);

    // First search the established-socket hash table
    if (established_socks[hashval]!=NULL){
        tju_handle_packet(established_socks[hashval], pkt);
        return;
    }

    // If none is found, search the listening-socket hash table
    hashval = cal_hash(local_ip, local_port, 0, 0); //A listening socket only has a local listening IP and port, with no remote endpoint
    if (listen_socks[hashval]!=NULL){
        tju_handle_packet(listen_socks[hashval], pkt);
        return;
    }

    // If neither lookup succeeds, drop the packet
    printf("No socket can handle this TCP packet; dropping the packet\n");
    return;
}



/*
Takes the user-filled TCP packet as an argument
Sends the datagram to the TCP destination IP and destination port provided by the user
Do not modify this function implementation
*/
void sendToLayer3(char* packet_buf, int packet_len){
    if (packet_len>MAX_LEN){
        printf("ERROR: cannot send packets longer than MAX_LEN, to prevent IP-layer fragmentation\n");
        return;
    }

    // Get the hostname and use it to determine whether this host is the client or the server
    char hostname[8];
    gethostname(hostname, 8);

    struct sockaddr_in conn;
    conn.sin_family      = AF_INET;            
    conn.sin_port        = htons(20218);
    int rst;
    if(strcmp(hostname,"server")==0){
        conn.sin_addr.s_addr = inet_addr(CLIENT_IP);
        rst = sendto(BACKEND_UDPSOCKET_ID, packet_buf, packet_len, 0, (struct sockaddr*)&conn, sizeof(conn));
    }else if(strcmp(hostname,"client")==0){       
        conn.sin_addr.s_addr = inet_addr(SERVER_IP);
        rst = sendto(BACKEND_UDPSOCKET_ID, packet_buf, packet_len, 0, (struct sockaddr*)&conn, sizeof(conn));
    }else{
        printf("Please do not change the hostname...\n");
        exit(-1);
    }
}

/*
 Simulated data receive thread
 Continuously calls recvfrom on the UDP socket where the server or client listens on port 20218
 Once data larger than the TCP header length is received
 receive the entire TCP packet and call onTCPPocket()
*/
void* receive_thread(void* arg){

    char hdr[DEFAULT_HEADER_LEN];
    char* pkt;

    uint32_t plen = 0, buf_size = 0, n = 0;
    int len;

    struct sockaddr_in from_addr;
    int from_addr_size = sizeof(from_addr);

    while(1) {
        // MSG_PEEK means inspect without removing data from the buffer
        len = recvfrom(BACKEND_UDPSOCKET_ID, hdr, DEFAULT_HEADER_LEN, MSG_PEEK, (struct sockaddr *)&from_addr, &from_addr_size);
        // Once data larger than the header length is received, receive the entire TCP packet
        if(len >= DEFAULT_HEADER_LEN){
            plen = get_plen(hdr); 
            pkt = malloc(plen);
            buf_size = 0;
            while(buf_size < plen){ // Continue until plen bytes have been received; all received data is stored in pkt
                n = recvfrom(BACKEND_UDPSOCKET_ID, pkt + buf_size, plen - buf_size, NO_FLAG, (struct sockaddr *)&from_addr, &from_addr_size);
                buf_size = buf_size + n;
            }
            // Notify the kernel that a complete TCP packet was received
            onTCPPocket(pkt);
            free(pkt);
        }
    }
}

/*
 Start the simulation and launch the background thread

 Whether this host is the server or the client
 create a UDP socket listening on port 20218
 then create a new thread that continuously calls recvfrom on that socket
*/
void startSimulation(){
    // Initialize the listening-socket and established-socket hash tables for the kernel
    int index;
    for(index=0;index<MAX_SOCK;index++){
        listen_socks[index] = NULL;
        established_socks[index] = NULL;
    }

    // Get the hostname
    char hostname[8];
    gethostname(hostname, 8);
    // printf("startSimulation on hostname: %s\n", hostname);

    BACKEND_UDPSOCKET_ID = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (BACKEND_UDPSOCKET_ID < 0){
        printf("ERROR opening socket");
        exit(-1);
    }

    // Set socket option SO_REUSEADDR = 1
    // This allows local address binding conflicts and changes how the system treats sockets in TIME_WAIT state
    int optval = 1;
    setsockopt(BACKEND_UDPSOCKET_ID, SOL_SOCKET, SO_REUSEADDR, (const void *)&optval , sizeof(int));

    struct sockaddr_in conn;
    memset(&conn, 0, sizeof(conn)); 
    conn.sin_family = AF_INET;
    conn.sin_addr.s_addr = htonl(INADDR_ANY); // INADDR_ANY = 0.0.0.0
    conn.sin_port = htons((unsigned short)20218);

    if (bind(BACKEND_UDPSOCKET_ID, (struct sockaddr *) &conn, sizeof(conn)) < 0){
        printf("ERROR on binding");
        exit(-1);
    }

    pthread_t thread_id = 1001;
    int rst = pthread_create(&thread_id, NULL, receive_thread, (void*)(&BACKEND_UDPSOCKET_ID));
    if (rst<0){
        printf("ERROR open thread");
        exit(-1); 
    }
    // printf("successfully created bankend thread\n");
    return;
}

int cal_hash(uint32_t local_ip, uint16_t local_port, uint32_t remote_ip, uint16_t remote_port){
    // The real implementation certainly would not calculate it this way
    return ((int)local_ip+(int)local_port+(int)remote_ip+(int)remote_port)%MAX_SOCK;
}