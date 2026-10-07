#ifndef _KERNEL_H_
#define _KERNEL_H_

#include "global.h"
#include "tju_packet.h"
#include <unistd.h>
#include "tju_tcp.h"

#define MAX_SOCK 32
tju_tcp_t* listen_socks[MAX_SOCK];
tju_tcp_t* established_socks[MAX_SOCK];

/*
Handler that simulates the Linux kernel receiving a TCP packet
*/
void onTCPPocket(char* pkt);


/*
Takes the user-filled TCP packet as an argument
Sends the datagram to the TCP destination IP and destination port provided by the user
*/
void sendToLayer3(char* packet_buf, int packet_len);


/*
Start the simulation and launch the background thread
*/
void startSimulation();


/*
 Thread that receives data over UDP
*/
void* receive_thread(void * in);

// Identifier of the socket that receives UDP packets
int BACKEND_UDPSOCKET_ID;


/*
 The Linux kernel uses
 local IP, local port, remote IP, and remote port to compute a four-tuple hash value
 to find the unique socket

 (In reality sockets are distinguished by a five-tuple
  which also includes a protocol field
  but since this project is TCP-only and the protocol is always the same, it is unnecessary here)
*/
int cal_hash(uint32_t local_ip, uint16_t local_port, uint32_t remote_ip, uint16_t remote_port);

#endif