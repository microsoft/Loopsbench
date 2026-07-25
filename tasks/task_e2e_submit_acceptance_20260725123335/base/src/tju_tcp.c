#include "tju_tcp.h"
#include <sys/time.h>
sendTimer* timerQueue = NULL;
/*
创建 TCP socket
初始化对应的结构体
设置初始状态为 CLOSED
*/
tju_tcp_t* tju_socket()
{
    /* TODO: implement */
    return NULL;
}

/*
绑定监听的地址 包括ip和端口
*/
int tju_bind(tju_tcp_t* sock, tju_sock_addr bind_addr)
{
    sock->bind_addr = bind_addr;
    return 0;
}

/*
被动打开 监听bind的地址和端口
设置socket的状态为LISTEN
注册该socket到内核的监听socket哈希表
*/
int tju_listen(tju_tcp_t* sock)
{
    sock->state = LISTEN;
    int hashval = cal_hash(sock->bind_addr.ip, sock->bind_addr.port, 0, 0);
    listen_socks[hashval] = sock;
    // 建立半连接和全连接队列
    syn_queues[hashval] = malloc(sizeof(syn_queue_member*) * MAX_SYN_QUEUE_LENGTH);
    accept_queues[hashval] = malloc(sizeof(tju_tcp_t*) * MAX_ACCEPT_QUEUE_LENGTH);
    return 0;
}

/*
接受连接
返回与客户端通信用的socket
这里返回的socket一定是已经完成3次握手建立了连接的socket
因为只要该函数返回, 用户就可以马上使用该socket进行send和recv
*/
tju_tcp_t* tju_accept(tju_tcp_t* listen_sock)
{
    /* TODO: implement */
    return NULL;
}

/*
连接到服务端
该函数以一个socket为参数
调用函数前, 该socket还未建立连接
函数正常返回后, 该socket一定是已经完成了3次握手, 建立了连接
因为只要该函数返回, 用户就可以马上使用该socket进行send和recv
*/
int tju_connect(tju_tcp_t* sock, tju_sock_addr target_addr)
{
    /* TODO: implement */
    return -1;
}

void* send_thread(void* args)
{
    /* TODO: implement */
    return NULL;
}

// 将新的计时器加入计时器队列
void timerAppend(sendTimer* T)
{
    /* TODO: implement */
}

// 删除首计时器节点
void timerDel()
{
    /* TODO: implement */
}

int tju_send(tju_tcp_t* sock, const void* buffer, int len)
{
    /* TODO: implement */
    return -1;
}

int tju_recv(tju_tcp_t* sock, void* buffer, int len)
{
    /* TODO: implement */
    return -1;
}

uint16_t count_rwnd(tju_tcp_t* sock)
{
    return TCP_RECVWN_SIZE - sock->received_len; // 虽然received_len 是int,但大小肯定在uint16的范围内
}

int tju_handle_packet(tju_tcp_t* sock, char* pkt)
{
    /* TODO: implement */
    return 0;
}
int tju_close(tju_tcp_t* sock)
{
    /* TODO: implement */
    return -1;
}

double timeval_diff(struct timeval* tv0, struct timeval* tv1)
{
    /* TODO: implement */
    return 0;
}

// 最初对计时器的设想，后来采用了计时器队列用于rdt，这部分计时器的设计便只用于实验的第一阶段了
// 该计时器递归调用重传
void* set_timer_with_retransmit_thread(void* args)
{
    /* TODO: implement */
    return NULL;
}

// 面对丢包时有处理，但不适应于滑动窗口的数据传输，滑动窗口的数据传输依靠sender_window的变量来协调
void packet_buf_send_with_timer(char* packet_buf, int packet_len, double timeoutInterval)
{
    /* TODO: implement */
}

long getCurrentTime()
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec * 1000000 + tv.tv_usec;
}

void write_log_SEND(uint32_t seq, uint32_t ack, uint8_t flag, int size)
{
    char hostname[8];
    gethostname(hostname, 8);
    long current_time = getCurrentTime();
    char flag_str[10];
    if (flag == ACK)
        strcpy(flag_str, "ACK");
    else if (flag == SYN)
        strcpy(flag_str, "SYN");
    else if (flag == ACK_SYN)
        strcpy(flag_str, "ACK|SYN");
    else if (flag == FIN_ACK)
        strcpy(flag_str, "FIN|ACK");

    if (strcmp(hostname, "server") == 0) {
        fprintf(server_event_log, "[%ld] [SEND] [seq:%d ack:%d flag:%d length:%d]\n", current_time, seq, ack, flag, size);
        // fflush(server_event_log);
    } else if (strcmp(hostname, "client") == 0) {
        fprintf(client_event_log, "[%ld] [SEND] [seq:%d ack:%d flag:%d length:%d]\n", current_time, seq, ack, flag, size);
        // fflush(client_event_log);
    }
}

void write_log_RECV(uint32_t seq, uint32_t ack, uint8_t flag, int size)
{
    char hostname[8];
    gethostname(hostname, 8);
    long current_time = getCurrentTime();
    char flag_str[10];
    if (flag == ACK)
        strcpy(flag_str, "ACK");
    else if (flag == SYN)
        strcpy(flag_str, "SYN");
    else if (flag == ACK_SYN)
        strcpy(flag_str, "ACK|SYN");
    else if (flag == FIN_ACK)
        strcpy(flag_str, "FIN|ACK");

    if (strcmp(hostname, "server") == 0) {
        fprintf(server_event_log, "[%ld] [RECV] [seq:%d ack:%d flag:%d length:%d]\n", current_time, seq, ack, flag, size);
        // fflush(server_event_log);
    } else if (strcmp(hostname, "client") == 0) {
        fprintf(client_event_log, "[%ld] [RECV] [seq:%d ack:%d flag:%d length:%d]\n", current_time, seq, ack, flag, size);
        // fflush(client_event_log);
    }
}

void write_log_CWND(int type, int size)
{
    char hostname[8];
    gethostname(hostname, 8);
    long current_time = getCurrentTime();

    if (strcmp(hostname, "server") == 0) {
        fprintf(server_event_log, "[%ld] [CWND] [type:%d size:%d]\n", current_time, type, size);
        //  fflush(server_event_log);
    } else if (strcmp(hostname, "client") == 0) {
        fprintf(client_event_log, "[%ld] [CWND] [type:%d size:%d]\n", current_time, type, size);
        // fflush(client_event_log);
    }
}

void write_log_RWND(int size)
{
    char hostname[8];
    gethostname(hostname, 8);
    long current_time = getCurrentTime();

    if (strcmp(hostname, "server") == 0) {
        fprintf(server_event_log, "[%ld] [RWND] [size:%d]\n", current_time, size);
        //  fflush(server_event_log);
    } else if (strcmp(hostname, "client") == 0) {
        fprintf(client_event_log, "[%ld] [RWND] [size:%d]\n", current_time, size);
        // fflush(client_event_log);
    }
}

void write_log_DELV(uint32_t seq, int size)
{
    char hostname[8];
    gethostname(hostname, 8);
    long current_time = getCurrentTime();

    if (strcmp(hostname, "server") == 0) {
        fprintf(server_event_log, "[%ld] [DELV] [seq:%d size:%d]\n", current_time, seq, size);
        // fflush(server_event_log);
    } else if (strcmp(hostname, "client") == 0) {
        fprintf(client_event_log, "[%ld] [DELV] [seq:%d size:%d]\n", current_time, seq, size);
        // fflush(client_event_log);
    }
}

void write_log_SWND(int size)
{
    char hostname[8];
    gethostname(hostname, 8);
    long current_time = getCurrentTime();

    if (strcmp(hostname, "server") == 0) {
        fprintf(server_event_log, "[%ld] [SWND] [size:%d]\n", current_time, size);
        // fflush(server_event_log);
    } else if (strcmp(hostname, "client") == 0) {
        fprintf(client_event_log, "[%ld] [SWND] [size:%d]\n", current_time, size);
        // fflush(client_event_log);
    }
}

void write_log_RTTS(float SampleRTT, float EstimatedRTT, float DeviationRTT, float TimeoutInterval)
{
    char hostname[8];
    gethostname(hostname, 8);
    long current_time = getCurrentTime();

    if (strcmp(hostname, "server") == 0) {
        fprintf(server_event_log, "[%ld] [RTTS] [SampleRTT:%f EstimatedRTT:%f DeviationRTT:%f TimeoutInterval:%f]\n", current_time, SampleRTT, EstimatedRTT, DeviationRTT, TimeoutInterval);
        // fflush(server_event_log);
    } else if (strcmp(hostname, "client") == 0) {
        fprintf(client_event_log, "[%ld] [RTTS] [SampleRTT:%f EstimatedRTT:%f DeviationRTT:%f TimeoutInterval:%f]\n", current_time, SampleRTT, EstimatedRTT, DeviationRTT, TimeoutInterval);
        // fflush(client_event_log);
    }
}

// 因为发送窗口和接受窗口都是一个循环数组的思想，指针在尽头需要回到缓冲区开头
// 循环数组中两个指针之间的距离，其中lower是要实际小于higher的
int distance_in_sender_window(char* lower, char* higher)
{
    /* TODO: implement */
    return 0;
}

// 因为发送窗口和接受窗口都是一个循环数组的思想，指针在尽头需要回到缓冲区开头
// 返回的是pos指针增加了num后的结果指针的位置
char* add_in_sender_window(tju_tcp_t* sock, char* pos, int num)
{
    /* TODO: implement */
    return NULL;
}