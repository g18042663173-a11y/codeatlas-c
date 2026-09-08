/* CC0-1.0: UTF-16 escapes and invalid surrogate pairs. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
struct vector { const char *json,*utf8; };
int main(void) {
    const struct vector valid[]={
        {"\"\\u0041\"","A"},{"\"\\u20ac\"","\xe2\x82\xac"},
        {"\"\\uD834\\uDD1E\"","\xf0\x9d\x84\x9e"},
        {"\"prefix \\uD83D\\uDE00 suffix\"","prefix \xf0\x9f\x98\x80 suffix"}
    };
    for(unsigned i=0;i<sizeof(valid)/sizeof(valid[0]);++i) {
        cJSON *value=cJSON_Parse(valid[i].json);
        assert(value && cJSON_IsString(value) && strcmp(value->valuestring,valid[i].utf8)==0);
        printf("accepted input=%s utf8_hex=",valid[i].json);
        for(const unsigned char *p=(const unsigned char *)value->valuestring;*p;++p) printf("%02x",*p);
        printf("\n");cJSON_Delete(value);
    }
    const char *invalid[]={"\"\\uDD1E\"","\"\\uD834\"","\"\\uD834\\u0041\"","\"\\uD834\\uD834\"","\"\\u12\""};
    for(unsigned i=0;i<sizeof(invalid)/sizeof(invalid[0]);++i) {
        const char *end=NULL;
        cJSON *value=cJSON_ParseWithLengthOpts(invalid[i],strlen(invalid[i])+1,&end,1);
        assert(value==NULL);
        printf("rejected input=%s error_offset=%td\n",invalid[i],end-invalid[i]);
    }
    /* This pinned revision maps invalid hex to zero; do not claim rejection. */
    cJSON *malformed=cJSON_Parse("\"\\uZZZZ\"");
    assert(malformed && cJSON_IsString(malformed) && malformed->valuestring[0]=='\0');
    printf("malformed_hex input=\"\\uZZZZ\" accepted=1 decoded_strlen=%zu first_byte=%02x\n",strlen(malformed->valuestring),(unsigned char)malformed->valuestring[0]);
    cJSON_Delete(malformed);
    return 0;
}
