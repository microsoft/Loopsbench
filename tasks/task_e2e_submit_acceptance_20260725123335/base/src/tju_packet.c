#include "tju_packet.h"


/*
 输入header所有字段 和 TCP包数据内容及其长度
 构造tju_packet_t
 返回其指针
 */
tju_packet_t* create_packet(uint16_t src, uint16_t dst, uint32_t seq,
    uint32_t ack, uint16_t hlen, uint16_t plen, uint8_t flags,
    uint16_t adv_window, uint8_t ext, char* data, int len){
    /* TODO: implement */
    return NULL;
}

/*
 输入header所有字段 和 TCP包数据内容及其长度
 构造tju_packet_t 
 返回其对应的字符串
 */
char* create_packet_buf(uint16_t src, uint16_t dst, uint32_t seq, uint32_t ack,
    uint16_t hlen, uint16_t plen, uint8_t flags, uint16_t adv_window,
    uint8_t ext, char* data, int len){
    /* TODO: implement */
    return NULL;
}

/*
 清除一个tju_packet_t的内存占用
 */
void free_packet(tju_packet_t* packet){
    if(packet->data != NULL)
         free(packet->data);
    free(packet);
}


/*
 下面的函数全部都是从一个packet的字符串中
 根据各个字段的偏移量
 找到并返回对应的字段
*/ 

uint16_t get_src(char* msg){
    /* TODO: implement */
    return 0;
}
uint16_t get_dst(char* msg){
    /* TODO: implement */
    return 0;
}
uint32_t get_seq(char* msg){
    /* TODO: implement */
    return 0;
}
uint32_t get_ack(char* msg){
    /* TODO: implement */
    return 0;
}
uint16_t get_hlen(char* msg){
    /* TODO: implement */
    return 0;
}
uint16_t get_plen(char* msg){
    /* TODO: implement */
    return 0;
}
uint8_t get_flags(char* msg){
    /* TODO: implement */
    return 0;
}
uint16_t get_advertised_window(char* msg){
    /* TODO: implement */
    return 0;
}
uint8_t get_ext(char* msg){
    /* TODO: implement */
    return 0;
}



/*############################################## 下面是实现上面函数功能的辅助函数 用户没必要调用 ##############################################*/


/*
 传入header所需的各种数据
 构造并返回header的字符串
 */
char* header_in_char(uint16_t src, uint16_t dst, uint32_t seq, uint32_t ack,
    uint16_t hlen, uint16_t plen, uint8_t flags, uint16_t adv_window,
    uint8_t ext){
    /* TODO: implement */
    return NULL;
}

/*
 根据传入的tju_packet_t指针
 构造并返回对应的字符串
 */
char* packet_to_buf(tju_packet_t* p){
    /* TODO: implement */
    return NULL;
}
