#include "mini.h"

#include <stdlib.h>

static MiniNode *allocate_node(int value)
{
    MiniNode *node = (MiniNode *)malloc(sizeof(MiniNode));
    if (node != NULL) {
        node->value = value;
        node->next = NULL;
    }
    return node;
}

MiniNode *cJSON_New_Item(void)
{
    return allocate_node(0);
}

void cJSON_Delete(MiniNode *node)
{
    while (node != NULL) {
        MiniNode *next = node->next;
        free(node);
        node = next;
    }
}

int parse_string(const char *text)
{
    return text != NULL && text[0] == '"';
}

int parse_number(const char *text)
{
    return text != NULL && text[0] >= '0' && text[0] <= '9';
}

int parse_object(const char *text)
{
    return text != NULL && text[0] == '{';
}

int parse_value(int value)
{
    if (value < 0) {
        return 0;
    }
    return value + 1;
}

MiniNode *cJSON_ParseWithOpts(const char *input, const char **end, int require_null_terminated)
{
    MiniNode *item = cJSON_New_Item();
    int valid = parse_string(input) || parse_number(input) || parse_object(input);
    if (end != NULL) {
        *end = input;
    }
    if (!valid || (require_null_terminated && input == NULL)) {
        cJSON_Delete(item);
        return NULL;
    }
    item->value = parse_value(valid);
    return item;
}

int cJSON_AddItemToObject(MiniNode *object, MiniNode *item)
{
    if (object == NULL || item == NULL) {
        return 0;
    }
    object->next = item;
    return 1;
}

MiniNode *cJSON_GetArrayItem(MiniNode *array, int index)
{
    while (array != NULL && index-- > 0) {
        array = array->next;
    }
    return array;
}

int callback_user(callback_t callback, int value)
{
    return callback(value);
}

static int cleanup_a(void)
{
    cJSON_Delete(cJSON_New_Item());
    return 1;
}

static int cleanup_b(void)
{
    cJSON_Delete(cJSON_New_Item());
    return 1;
}

static int cleanup_c(void)
{
    cJSON_Delete(cJSON_New_Item());
    return 1;
}

static int cleanup_d(void)
{
    cJSON_Delete(cJSON_New_Item());
    return 1;
}

static int cleanup_e(void)
{
    cJSON_Delete(cJSON_New_Item());
    return 1;
}

static int cleanup_f(void)
{
    cJSON_Delete(cJSON_New_Item());
    return 1;
}

int run_all(void)
{
    callback_t callback = parse_value;
    return cleanup_a() + cleanup_b() + cleanup_c() + cleanup_d() + cleanup_e() + cleanup_f()
        + callback_user(callback, 1);
}
