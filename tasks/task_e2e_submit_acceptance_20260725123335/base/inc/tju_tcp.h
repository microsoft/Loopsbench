#ifndef _TJU_TCP_H_
#define _TJU_TCP_H_

#include "global.h"
#include "kernel.h"
#include "tju_packet.h"

/*
new added: 全连接队列和半连接队列
*/
typedef struct {
    tju_tcp_t* sock;
    uint32_t expect_ack;
} syn_queue_member;

syn_queue_member** syn_queues[32]; // 半连接
tju_tcp_t** accept_queues[32];     // 全连接

FILE* server_event_log;
FILE* client_event_log;

#define MAX_SYN_QUEUE_LENGTH 32
#define MAX_ACCEPT_QUEUE_LENGTH 32

typedef struct {
    char* buf;
    int len;
    double timeout;
} timer_args;

int receive_flag; // 为1表示收到ACK了，这里觉得应该搞一个数组，每个socket对应一个，这里为了简便
int tmp_ack;
int last_fast_retransmit_seq;

typedef struct {
    uint32_t ack; // 记录所发送pkt的Ack字段值
    uint32_t seq; // 记录所发送pkt的Seq字段值
    char* firstByte;
    int len;
    int is_retransmit; // 记录下这个timer所对应的pkt是否被重传过，仅仅只有接收端哦未被重传过的数据包时才会用于更新timeout_interval
    uint8_t flags;     // 标志位

    struct timeval sendTime;
    struct timeval updateTime; // 当计时器队列队首的计时器取消后，计时器应当重置 // 仅用于send_thread的超时判断
    struct timeval RTO;
    struct sendTimer* next;
} sendTimer;

#define timercpy(dst, src)               \
    do {                                 \
        (dst)->tv_sec = (src)->tv_sec;   \
        (dst)->tv_usec = (src)->tv_usec; \
    } while (0)

// new added
void write_log_DELV(uint32_t seq, int size);
void write_log_RTTS(float SampleRTT, float EstimatedRTT, float DeviationRTT, float TimeoutInterval);
void timerAppend(sendTimer* T);
void timerDel();
double timeval_diff(struct timeval* tv0, struct timeval* tv1);
void* set_timer_with_retransmit_thread(void* args);
void packet_buf_send_with_timer(char* packet_buf, int packet_len, double timeoutInterval);
void* send_thread(void* args);
uint16_t count_rwnd(tju_tcp_t* sock);
long getCurrentTime();
void write_log_SEND(uint32_t seq, uint32_t ack, uint8_t flag, int size);
void write_log_RECV(uint32_t seq, uint32_t ack, uint8_t flag, int size);
void write_log_CWND(int type, int size);
void write_log_RWND(int size);
void write_log_SWND(int size);
char* add_in_sender_window(tju_tcp_t* sock, char* pos, int num);
int distance_in_sender_window(char* lower, char* higher);
/*
创建 TCP socket
初始化对应的结构体
设置初始状态为 CLOSED
*/
tju_tcp_t* tju_socket();

/*
绑定监听的地址 包括ip和端口
*/
int tju_bind(tju_tcp_t* sock, tju_sock_addr bind_addr);

/*
被动打开 监听bind的地址和端口
设置socket的状态为LISTEN
*/
int tju_listen(tju_tcp_t* sock);

/*
接受连接
返回与客户端通信用的socket
这里返回的socket一定是已经完成3次握手建立了连接的socket
因为只要该函数返回, 用户就可以马上使用该socket进行send和recv
*/
tju_tcp_t* tju_accept(tju_tcp_t* sock);

/*
连接到服务端
该函数以一个socket为参数
调用函数前, 该socket还未建立连接
函数正常返回后, 该socket一定是已经完成了3次握手, 建立了连接
因为只要该函数返回, 用户就可以马上使用该socket进行send和recv
*/
int tju_connect(tju_tcp_t* sock, tju_sock_addr target_addr);

int tju_send(tju_tcp_t* sock, const void* buffer, int len);
int tju_recv(tju_tcp_t* sock, void* buffer, int len);

/*
关闭一个TCP连接
这里涉及到四次挥手
*/
int tju_close(tju_tcp_t* sock);

int tju_handle_packet(tju_tcp_t* sock, char* pkt);
#endif