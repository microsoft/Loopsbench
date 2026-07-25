#ifndef _GLOBAL_H_
#define _GLOBAL_H_

#include <netinet/in.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "global.h"
#include <pthread.h>
#include <sys/select.h>
#include <arpa/inet.h>
#include <math.h>

#define CLIENT_IP "172.17.0.2"
#define SERVER_IP "172.17.0.3"
// 单位是byte
#define SIZE32 4
#define SIZE16 2
#define SIZE8  1

// 一些Flag
#define NO_FLAG 0
#define NO_WAIT 1
#define TIMEOUT 2
#define TRUE 1
#define FALSE 0

// new added 包头flag位
#define SYN 8      // 0b00001000
#define ACK 4      // 0b00000100
#define ACK_SYN 12 // 0b00001100
#define FIN 2      // 0b00000010
#define FIN_ACK 6  // 0b00000110

#define __MYDEBUG__
#ifdef __MYDEBUG__
#define DPRINTF(...) printf(__VA_ARGS__)
#else
#define DPRINTF(...)
#endif

// 定义最大包长 防止IP层分片
#define MAX_DLEN 1375 	// 最大包内数据长度
#define MAX_LEN 1400 	// 最大包长度

// TCP socket 状态定义
#define CLOSED 0
#define LISTEN 1
#define SYN_SENT 2
#define SYN_RECV 3
#define ESTABLISHED 4
#define FIN_WAIT_1 5
#define FIN_WAIT_2 6
#define CLOSE_WAIT 7
#define CLOSING 8
#define LAST_ACK 9
#define TIME_WAIT 10

// TCP 拥塞控制状态
#define SLOW_START 0
#define CONGESTION_AVOIDANCE 1
#define FAST_RECOVERY 2
#define CONGESTION_TIMEOUT 3

#define FLOW_CONTROL // 通过rwnd流量控制来控制发送速率
// #define MAX_SEND_PKT_CONTROL // 通过限制最大已发送未确认数据包的数量来控制发送速率
#define CONGESTION_CONTROL

pthread_t CLIENT_SEND_PTHREAD_ID;
pthread_t SERVER_SEND_PTHREAD_ID;

// TCP 接受窗口大小
#define TCP_RECVWN_SIZE 47 * MAX_DLEN  // 比如最多放32个满载数据包

// TCP  发送窗口大小
#define TCP_SENDWN_SIZE 64 * MAX_DLEN // 比如最多放32个满载数据包
// #define TCP_SENDWN_SIZE 50 * 1024 * 1024

#define MAX_SEND_PKT 64
int send_pkt_num;




// TCP 发送窗口
// 注释的内容如果想用就可以用 不想用就删掉 仅仅提供思路和灵感
typedef struct {
    char buf[TCP_SENDWN_SIZE];

    uint16_t window_size;

    char* base_ptr;    // 指向base的指针
    char* nextseq_ptr; // 指向下一个待发送的
    uint32_t base;
    uint32_t nextseq;

    struct timeval estmated_rtt;     // 初始值设为0  （不确定是不是这样）
    struct timeval dev_rtt;          // 初始值设为0  （不确定是不是这样）
    struct timeval timeout_interval; // 初始值设为1 就是 RTO

    pthread_mutex_t timer_lock; // 计时器锁

    uint32_t ack_sent; // 发送数据包时，包头Ack位的值

    int ack_cnt; // 收到的冗余ack数量
    pthread_mutex_t ack_cnt_lock;

    struct timeval send_time;
    struct timeval timeout;
    int is_timer_set;
    uint32_t timer_expect_ack; // 期待收到的ack， 一旦收到这个ack即刻停止计时器
    int is_timer_stop;

    uint32_t rwnd; // 对面的rwnd，对面通过ACK的rwnd来通知

    int congestion_status;
    int cwnd;
    int ssthresh;
} sender_window_t;

// TCP 接受窗口
// 注释的内容如果想用就可以用 不想用就删掉 仅仅提供思路和灵感
typedef struct {
    char buf[TCP_RECVWN_SIZE];

    uint32_t expect_seq; // 期待收到的msg的Seq， 以防失序用

} receiver_window_t;

// TCP 窗口 每个建立了连接的TCP都包括发送和接受两个窗口
typedef struct {
    sender_window_t* wnd_send;
    receiver_window_t* wnd_recv;
} window_t;

typedef struct {
    uint32_t ip;
    uint16_t port;
} tju_sock_addr;

// TJU_TCP 结构体 保存TJU_TCP用到的各种数据
typedef struct {
    int state; // TCP的状态

    tju_sock_addr bind_addr;               // 存放bind和listen时该socket绑定的IP和端口
    tju_sock_addr established_local_addr;  // 存放建立连接后 本机的 IP和端口
    tju_sock_addr established_remote_addr; // 存放建立连接后 连接对方的 IP和端口

    pthread_mutex_t send_lock; // 发送数据锁
    char* sending_buf;         // 发送数据缓存区
    int sending_len;           // 发送数据缓存长度

    pthread_mutex_t recv_lock; // 接收数据锁
    char* received_buf;        // 接收数据缓存区
    int received_len;          // 接收数据缓存长度

    pthread_cond_t wait_cond; // 可以被用来唤醒recv函数调用时等待的线程

    window_t window; // 发送和接受窗口

} tju_tcp_t;

#endif