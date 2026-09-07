#include "mini.h"

static MiniNode *global_node;

int inspect_global_node(void)
{
    if (global_node != 0) {
        return global_node->value;
    }
    return 0;
}
