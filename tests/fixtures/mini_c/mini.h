#ifndef CODEATLAS_MINI_H
#define CODEATLAS_MINI_H

typedef int (*callback_t)(int value);

typedef struct MiniNode {
    int value;
    struct MiniNode *next;
} MiniNode;

MiniNode *cJSON_New_Item(void);
void cJSON_Delete(MiniNode *node);
MiniNode *cJSON_ParseWithOpts(const char *input, const char **end, int require_null_terminated);
int parse_value(int value);
int parse_string(const char *text);
int parse_number(const char *text);
int parse_object(const char *text);
int cJSON_AddItemToObject(MiniNode *object, MiniNode *item);
MiniNode *cJSON_GetArrayItem(MiniNode *array, int index);
int callback_user(callback_t callback, int value);
int run_all(void);

#endif
