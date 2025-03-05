#!/usr/bin/env python3
import argparse
import base64
import gc
import io
import json
import os
import sys
import time

import PIL as pillow
from PIL import Image, PngImagePlugin
from diffusers import FluxTransformer2DModel, FluxPipeline
from loguru import logger
from pydantic import BaseModel
from transformers import T5EncoderModel, CLIPTextModel
from typing import Optional
import openai
import optimum.quanto
import torch
import uvicorn

import openedai

default_config_template = 'config.default.json'
default_config_json = 'config/config.json'
no_enhance_prompt = "I NEED to test how the tool works with extremely simple prompts. DO NOT add any detail, just use it AS-IS:"
pipe_global = None
generator_name_global = None
random_seed = -1
app = openedai.OpenAIStub()

map_qdtype = dict([ (name, optimum.quanto.qtypes[name]) for name in optimum.quanto.qtypes ] +
     [('fp8', optimum.quanto.qfloat8), ('int8', optimum.quanto.qint8), ('int4', optimum.quanto.qint4), ('int2', optimum.quanto.qint2)])

def quanto_wrap(model, quantize):
    if quantize:
        quant_kwargs = {}
        if isinstance(quantize, str):
            quant_kwargs['weights'] = map_qdtype[quantize]
        else:
            for i in ['weights', 'activations']:
                if i in quantize:
                    quant_kwargs[i] = map_qdtype[quantize[i]]

        optimum.quanto.quantize(model, **quant_kwargs)
        optimum.quanto.freeze(model)


# This defines the OpenAI API for /v1/images/generations endpoints
class GenerationsRequest(BaseModel):
    prompt: str # required? empty prompts are kinda cool.
    model: Optional[str] = "dall-e-2" # any
    size: Optional[str] = "1024x1024" # any
    quality: Optional[str] = "standard" # or hd, any
    response_format: Optional[str] = "url" # or b64_json
    n: Optional[int] = 1 # 1-10, 1 only for dall-e-3
    style: Optional[str] = "vivid" # natural, any
    user: Optional[str] = None


async def load_flux_model(config: dict) -> FluxPipeline:

    logger.debug(f"Loading flux model: config: {config}")

    pipeline = config.pop('pipeline') #
    options = config.pop('options', {})
    _ = config.pop('generation_kwargs', {})
    
    lora = None
    transformer = None
    text_encoder = None
    text_encoder_2 = None

    if 'FluxTransformer2DModel' in pipeline: # phased loading of models
        flux_transformer = pipeline.pop('FluxTransformer2DModel')
        if 'torch_dtype' in flux_transformer:
            flux_transformer['torch_dtype'] = getattr(torch, flux_transformer['torch_dtype'])
        if 'device' in flux_transformer:
            if isinstance(flux_transformer['device'], str):
                flux_transformer['device'] = getattr(torch, flux_transformer['device'])

        pipeline['transformer'] = None
        quantize = flux_transformer.pop('quantize', None)
        if 'pretrained_model_link_or_path_or_dict' in flux_transformer:
            transformer = FluxTransformer2DModel.from_single_file(**flux_transformer)
        else:
            transformer = FluxTransformer2DModel.from_pretrained(**flux_transformer)
        quanto_wrap(transformer, quantize)

    if 'T5EncoderModel' in pipeline:
        t5enc = pipeline.pop('T5EncoderModel')
        if 'torch_dtype' in t5enc:
            t5enc['torch_dtype'] = getattr(torch, t5enc['torch_dtype'])

        pipeline['text_encoder_2'] = None
        quantize = t5enc.pop('quantize', None)
        text_encoder_2 = T5EncoderModel.from_pretrained(**t5enc)
        quanto_wrap(text_encoder_2, quantize)

    if 'CLIPTextModel' in pipeline:
        clip = pipeline.pop('CLIPTextModel')
        if 'torch_dtype' in clip:
            clip['torch_dtype'] = getattr(torch, clip['torch_dtype'])

        pipeline['text_encoder'] = None
        quantize = clip.pop('quantize', None)
        text_encoder = CLIPTextModel.from_pretrained(**clip)
        quanto_wrap(text_encoder, quantize) # don't do this

    #if 'Loras' in pipeline
    loras = pipeline.pop("Loras", [])

    logger.debug(f"Loading {pipeline}")

    if 'torch_dtype' in pipeline:
        pipeline['torch_dtype'] = getattr(torch, pipeline['torch_dtype'])

    flux_pipe = FluxPipeline.from_pretrained(**pipeline)

    if transformer:
        flux_pipe.transformer = transformer
    if text_encoder:
        flux_pipe.text_encoder = text_encoder
    if text_encoder_2:
        flux_pipe.text_encoder_2 = text_encoder_2

    # Load/Run Options
    if 'enable_sequential_cpu_offload' in options and options['enable_sequential_cpu_offload']:
        if not isinstance(options['enable_sequential_cpu_offload'], dict):
            options['enable_sequential_cpu_offload'] = {}
        flux_pipe.enable_sequential_cpu_offload(**options['enable_sequential_cpu_offload'])
    if 'enable_model_cpu_offload' in options and options['enable_model_cpu_offload']:
        if not isinstance(options['enable_model_cpu_offload'], dict):
            options['enable_model_cpu_offload'] = {}
        flux_pipe.enable_model_cpu_offload(**options['enable_model_cpu_offload'])
    if options.get('enable_vae_slicing', False):
        flux_pipe.vae.enable_slicing()
    if options.get('enable_vae_tiling', False):
        flux_pipe.vae.enable_tiling()
    if 'to' in options:
        if 'dtype' in options['to']:
            options['to']['dtype'] = getattr(torch, options['to']['dtype'])
        flux_pipe.to(**options['to'])
    if options.get('fuse_qkv_projections', False):
        flux_pipe.transformer.fuse_qkv_projections()
        flux_pipe.vae.fuse_qkv_projections()

    # Loras
    for lora in loras:
        lora_weights = lora.pop('weights')

        logger.info(f"Loading Lora: args: {lora_weights['weight_name']}")
        flux_pipe.load_lora_weights(**lora_weights)
        if 'options' in lora:
            lora_scale=lora['options'].pop('lora_scale', 1.0)
        else:
            lora_scale=lora.pop('lora_scale', 1.0)
        flux_pipe.fuse_lora(lora_scale=lora_scale)
        flux_pipe.unload_lora_weights()

    compile = options.pop('compile', [])
    if 'transformer' in compile:
        logger.info(f"Torch compiling transformer ...")
        flux_pipe.transformer.to(memory_format=torch.channels_last)
        flux_pipe.transformer = torch.compile(flux_pipe.transformer, mode="max-autotune", fullgraph=True)
    if 'vae' in compile:
        logger.info(f"Torch compiling vae ...")
        flux_pipe.vae.to(memory_format=torch.channels_last)
        flux_pipe.vae = torch.compile(flux_pipe.vae, mode="max-autotune", fullgraph=True)


    return flux_pipe

def unload_model():
    global pipe_global, generator_name_global
    logger.info(f"UNLoading generator: {generator_name_global}")
    if pipe_global: del pipe_global
    pipe_global = None
    generator_name_global = None
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()
    torch.cuda.reset_max_memory_allocated()
    torch.cuda.reset_peak_memory_stats()

async def ready_model(generator_name: str, generator: dict) -> FluxPipeline:
    global pipe_global, generator_name_global
    if pipe_global is None:
        logger.info(f"Loading generator from: {generator_name}")
        pipe_global = await load_flux_model(generator)
        generator_name_global = generator_name

    elif generator_name != generator_name_global:
        unload_model()
        logger.info(f"Loading generator: {generator_name}")
        pipe_global = await load_flux_model(generator)
        generator_name_global = generator_name

    return pipe_global


def config_loader(file_path: str, model: str = 'dall-e-2') -> tuple:
    # walk the config file, load fragments and set defaults as needed
    # return the final model_config, generation_kwargs, enhancer 
    with open(file_path, 'r') as f:
        config = json.load(f)

    conf_folder = os.path.dirname(file_path)

    ### TODO: raise exceptions on bad config
    if not 'models' in config:
        raise openedai.InternalServerError("No models defined in config")

    if not model in config['models']:
        raise openedai.BadRequestError(f"Model not found in config: {model}", param='model')

    mconfig = config['models'][model]
    enhancer = mconfig.get('enhancer', None)
    model_config = mconfig.get('generator', None)

    if enhancer:
        enhancer = os.path.join(conf_folder, enhancer)
        with open(enhancer, 'r') as ef:
            enhancer = json.load(ef)
    
    if model_config:
        generator_name = model_config
        model_config = os.path.join(conf_folder, model_config)
        with open(model_config, 'r') as mcf:
            model_config = json.load(mcf)

    return generator_name, model_config, enhancer


def load_generation_config(request: GenerationsRequest) -> tuple:
    width, height = request.size.split('x')

    generation_kwargs = dict(
        prompt = request.prompt,
        width = 8 * (int(width) // 8),
        height = 8 * (int(height) // 8),
        num_images_per_prompt = request.n,
    )

    #style = request.style,
    #user = request.user,

    generator_name, generator, enhancer = config_loader(args.config, model=request.model)

    gen_kwargs = generator.pop('generation_kwargs', {})
    if request.quality in gen_kwargs:
        generation_kwargs.update(gen_kwargs[request.quality])
    else:
        ### Maybe needs more error checking here?
        generation_kwargs.update(gen_kwargs.get('standard', gen_kwargs)) # the default

    return generator_name, generator, generation_kwargs, enhancer


async def generate_images(pipe, **generation_kwargs) -> list:
    global random_seed
    # TODO: handle long prompts > 77 tokens in CLIP, >~250 in T5

    seed = random_seed if random_seed != -1 else int(time.time() * 1e6) & 0xFFFFFFFFFFFFFFFF

    logger.debug(f"generation_kwargs [seed={seed}]: {generation_kwargs}")

    generation_kwargs['generator'] = torch.Generator("cpu").manual_seed(seed)

    try:
        return pipe(**generation_kwargs).images, seed
    finally:
        torch.cuda.empty_cache()

async def enhance_prompt(prompt: str, **enhancer) -> str:
    enhancer['messages'].extend([{'role': 'user', 'content': prompt }])

    openai_params = {}
    base_url = enhancer.pop('OPENAI_BASE_URL', os.environ.get("OPENAI_BASE_URL", None))
    api_key = enhancer.pop('OPENAI_API_KEY', os.environ.get("OPENAI_API_KEY", None))
    if base_url:
        openai_params['base_url'] = base_url
    if api_key:
        openai_params['api_key'] = api_key
    else:
        return prompt

    resp = openai.OpenAI(**openai_params).chat.completions.create(**enhancer)
    return resp.choices[0].message.content


@app.post("/v1/images/generations")
async def generations(request: GenerationsRequest):
    resp = {
        'created': int(time.time() * 1000),
        'data': []
    }

    # block or queue requests?

    # unload hack
    if request.model == "unload":
        unload_model()
        return resp

    generator_name, model_config, generation_kwargs, enhancer = load_generation_config(request)

    # dall-e-3 reworks the prompt
    # https://platform.openai.com/docs/guides/images/prompting
    revised_prompt = None
    if request.prompt.startswith(no_enhance_prompt):
        generation_kwargs['prompt'] = request.prompt = request.prompt[len(no_enhance_prompt):]
        enhancer = None

    if enhancer:
        try:
            generation_kwargs['prompt'] = revised_prompt = await enhance_prompt(generation_kwargs['prompt'], **enhancer)
        except Exception as e:
            logger.warning(f"{repr(e)}. Enhancer failed: {enhancer}")
            logger.debug(e)

    try:
        pipe = await ready_model(generator_name, model_config)
        images, seed = await generate_images(pipe, **generation_kwargs)

        if images:
            for img in images:
                def make_pngmetadata():
                    # not sure how flux does it, but this is how SD did it.
                    # a closeup portrait of a playful maid, undercut hair, apron, amazing body, pronounced feminine feature, busty, kitchen, [ash blonde | ginger | pink hair], freckles, flirting with camera.Negative prompt: (deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation. tattoo.
                    # Steps: 30, Sampler: DPM++ 2M Karras, CFG scale: 6.5, Seed: 1804518985, Size: 768x1024, Model hash: 9aba26abdf, Model: Deliberate, ENSD: 31337
                    k = generation_kwargs
                    parameters = f"{k['prompt']}{'.' if not k['prompt'] or not k['prompt'][-1] else ''}Steps: {k['num_inference_steps']}, Sampler: Euler, CFG Scale: {k['guidance_scale']}, Seed: {seed}, Size: {k['width']}x{k['height']}, Model: {request.model}" # batch?
                    pngmetadata = PngImagePlugin.PngInfo()
                    pngmetadata.add_text('Parameters', parameters)
                    return pngmetadata

                pnginfo = make_pngmetadata()

                if args.log_level == 'DEBUG':
                    img.save("config/debug.png", pnginfo=pnginfo)

                img_bytes = io.BytesIO()
                img.save(img_bytes, format='PNG', pnginfo=pnginfo)
                b64_json = base64.b64encode(img_bytes.getvalue()).decode('utf-8')
                img_bytes.close()

                
                if request.response_format == 'b64_json':
                    img_dat = {'b64_json': b64_json}
                else:
                    img_dat = {'url': f'data:image/png;base64,{b64_json}'}  # yeah it's lazy. requests.get() will not work with this, but web clients will

                if revised_prompt:
                    img_dat['revised_prompt'] = revised_prompt

                resp['data'].extend([img_dat])

        logger.debug(f"Generated {len(images)} {request.model} image(s) in {time.time() - resp['created'] / 1000:.1f}s")

        return resp

    except Exception as e: 
        logger.error(e)
        message = repr(e)

    unload_model()
    raise openedai.InternalServerError(message)

def default_config_exists():
    if not os.path.exists(default_config_json):
        logger.info(f"Missing {default_config_json}, installing {default_config_template}")
        with open(default_config_template, 'r', encoding='utf8') as from_file:
            with open(default_config_json, 'w', encoding='utf8') as to_file:
                to_file.write(from_file.read())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='OpenedAI Images Flux API Server',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('-C', '--config', action='store', default=default_config_json, help="Path to the config json file")
    parser.add_argument('-S', '--seed', action='store', default=None, type=int, help="The random seed to set for all generations. (default is random)")
    parser.add_argument('-L', '--log-level', default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Set the log level")
    parser.add_argument('-P', '--port', action='store', default=5005, type=int, help="Server tcp port")
    parser.add_argument('-H', '--host', action='store', default='0.0.0.0', help="Host to listen on, Ex. 0.0.0.0")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])

    logger.remove()
    logger.add(sink=sys.stderr, level=args.log_level)

    logger.debug(f"args: {args}")

    default_config_exists()

    # tuning for compile
    torch._inductor.config.conv_1x1_as_mm = True
    torch._inductor.config.coordinate_descent_tuning = True
    torch._inductor.config.epilogue_fusion = False
    torch._inductor.config.coordinate_descent_check_all_directions = True

    # from hyperflux
    torch.backends.cuda.matmul.allow_tf32 = True

    def get_cuda_compute_capability():
        device = torch.cuda.current_device()
        properties = torch.cuda.get_device_properties(device)
        return properties.major, properties.minor

    # from sayakpaul/diffusers-torchao
    if get_cuda_compute_capability()[0] >= 8:
        torch.set_float32_matmul_precision("high")

    if args.seed is not None:
        random_seed = args.seed

    # load config
    if not os.path.exists(args.config):
        logger.error("Config file not found: {}".format(args.config))
        sys.exit(1)
    else:
        with open(args.config) as f:
            config = json.load(f)

    for m in config['models']:
        app.register_model(m)

    uvicorn.run(app, host=args.host, port=args.port)#
